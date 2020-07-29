import time
import os
import json

import numpy as np
import torch
import gym
from torch.utils.tensorboard import SummaryWriter

from ...common.utils import append_timestamp, reset_seeds, initialize_environment
from ...common.parsers import sarsa_parser
from .model import SarsaAgent


def test_policy(test_env, agent, episode, global_steps, writer, log_filename, args):
    with torch.no_grad():
        # Reset environment
        cumulative_reward = 0

        for _ in range(args.episodes_per_eval):
            test_state = test_env.reset()

            test_done = False
            render = args.render and (episode % args.render_episodes == 0)

            # Test episode loop
            while not test_done:
                test_action = agent.act(test_state, test_done, agent.epsilon)

                # Take action in env
                if render:
                    test_env.render()

                test_state, test_reward, test_done, _ = test_env.step(test_action)

                # Update reward
                cumulative_reward += test_reward
        eval_reward = cumulative_reward / args.episodes_per_eval

        print("Policy_reward for test:", eval_reward)

        # Logging
        if not args.no_tensorboard:
            writer.add_scalar('validation/policy_reward', eval_reward, global_steps)
        if log_filename:
            with open(log_filename, "a") as f:
                f.write("{},{},{},".format(episode, global_steps, eval_reward))


def episode_loop(env, test_env, agent, args, writer):
    # Episode loop
    global_steps = 0
    steps = 1
    episode = 0
    start = time.time()
    t_zero = time.time()

    end = time.time() + 1

    score = 0
    while global_steps < args.max_steps:
        info_str = "episode: {}, ".format(episode)
        info_str += "steps: {}, ".format(global_steps)
        info_str += "ep_score: {}, ".format(score)
        info_str += "FPS: {}".format(steps / (end - start))
        print(info_str)
        start = time.time()

        state = env.reset()
        done = False
        agent.set_epsilon(global_steps, writer)
        action = agent.act(state, done, agent.epsilon)

        steps = 1
        score = 0
        # Collect data from the environment
        while not done:

            next_state, reward, done, _ = env.step(action)
            score += reward
            steps += 1

            if args.reward_clip:
                clipped_reward = np.clip(reward, -args.reward_clip, args.reward_clip)
            else:
                clipped_reward = reward

            # Testing policy
            if global_steps % args.test_policy_steps == 0:
                test_policy(test_env, agent, episode, global_steps, writer, log_filename, args)
                if log_filename:
                    with open(log_filename, "a") as f:
                        f.write("{:.2f}\n".format(time.time() - t_zero))

            global_steps += 1

            # If not enough data, try again
            if global_steps < args.warmup_period:
                continue

            # Training loop
            action = agent.train_single_batch(state,
                                              next_state,
                                              action,
                                              clipped_reward,
                                              done,
                                              agent.epsilon)

            state = next_state

            if args.model_path is not None and global_steps % args.checkpoint_steps == 0:
                agent.save_checkpoint(episode, global_steps, args)

        end = time.time()

        episode += 1


args = sarsa_parser.parse_args()

# Set seeds
reset_seeds(args.seed)

# Initialize envs
env, test_env = initialize_environment(args)

if type(env.action_space) != gym.spaces.Discrete:
    raise NotImplementedError("DQN for continuous action_spaces hasn't been implemented")

# Check if GPU can be used and was asked for
if args.gpu and torch.cuda.is_available():
    device = torch.device('cuda:0')
else:
    device = torch.device('cpu')

# Initialize model
agent_args = {
    "device": device,
    "observation_space": env.observation_space,
    "action_space": env.action_space,
    "alpha": args.alpha,
    "gamma": args.gamma,
    "lambda_value": args.lambda_value,
    "feature_size": args.feature_size,
}
agent = SarsaAgent(**agent_args)

# Save path
if args.model_path:
    os.makedirs(args.model_path, exist_ok=True)

if args.output_path:
    os.makedirs(args.output_path, exist_ok=True)

# Logging via csv
if args.output_path:
    base_filename = os.path.join(args.output_path, args.run_tag)
    os.makedirs(base_filename, exist_ok=True)
    log_filename = os.path.join(base_filename, 'reward.csv')
    with open(log_filename, "w") as f:
        f.write("episode,steps,reward,runtime\n")
    with open(os.path.join(base_filename, 'params.json'), 'w') as fp:
        param_dict = vars(args).copy()
        del param_dict['output_path']
        del param_dict['model_path']
        json.dump(param_dict, fp)
else:
    log_filename = None

# Logging for tensorboard
if not args.no_tensorboard:
    writer = SummaryWriter(comment=args.run_tag)
else:
    writer = None

episode_loop(env, test_env, agent, args, writer)

env.close()
test_env.close()

if args.model_path:
    torch.save(agent.online, append_timestamp(os.path.join(args.model_path, args.run_tag)) + ".pth")
