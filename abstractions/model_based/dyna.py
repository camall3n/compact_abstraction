from collections import namedtuple
import math

import gym
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from abstractions.common.pqueue import PriorityQueue
from abstractions.common.replay_buffer import Experience
from abstractions.common.utils import model_based_parser, soft_update, hard_update
from abstractions.model_based.model import ModelNet
from abstractions.model_free.dqn.model import DQN_MLP_model

Trajectory = namedtuple('Trajectory',
    ('action', 'state', 'inner_return', 'final_state', 'done', 'n')
)

class DynaAgent:
    def __init__(self, state_space, action_space, gamma, args):
        self.state_space = state_space
        self.action_space = action_space
        self.gamma = gamma
        self.epsilon = 1.0
        self.epsilon_decay_rate = args.epsilon_decay_rate
        self.final_epsilon_value = args.final_epsilon_value
        self.device = args.device
        self.critic = DQN_MLP_model(
            args.device, state_space, action_space, action_space.n, args.model_shape
        )
        self.critic_target = DQN_MLP_model(
            args.device, state_space, action_space, action_space.n, args.model_shape
        )
        hard_update(self.critic_target, self.critic)
        self.target_moving_average = args.target_moving_average # future updates will be soft updates

        self.model = ModelNet(args, args.device, state_space, action_space).to(device=args.device)
        self.queries = PriorityQueue(maxlen=10000, mode='max')
        self.priority_threshold = args.priority_threshold
        self.priority_decay = args.priority_decay
        self.max_rollout_length = args.max_rollout_length
        self.warmup_period = args.warmup_period
        self.model_loss_threshold = args.model_loss_threshold

        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=args.lr)
        self.model_optimizer = torch.optim.Adam(self.model.parameters(), lr=args.model_lr)

        # Logging for tensorboard
        self.writer = None if args.no_tensorboard else SummaryWriter(comment=args.run_tag)
        self.global_step = 0

    def act(self, state, eval=False):
        if np.random.uniform() < self.epsilon:
            action = self.action_space.sample()
        else:
            with torch.no_grad():
                state = torch.from_numpy(state).float().to(self.device)
                action = torch.argmax(self.critic(state).detach(), dim=-1).cpu().item()
        if self.writer:
            self.writer.add_scalar('dyna/epsilon', self.epsilon, self.global_step)
        return action

    def train(self, experiences):
        batch = self._torchify_experience(Experience(*list(zip(*experiences))))
        td_errors = self.update_agent(batch)
        loss = self.update_model(batch)
        if loss < self.model_loss_threshold:
            for experience, td_error in zip(experiences, td_errors):
                self.queue_rollouts(experience, 0, td_error)
        if self.writer:
            self.writer.add_scalar('dyna/queue_length', len(self.queries), self.global_step)

    def test(self, test_env, n_episodes):
        with torch.no_grad():
            total_reward = 0
            for _ in range(n_episodes):
                state = test_env.reset()
                done = False
                while not done:
                    action = self.act(state)
                    next_state, reward, done, _ = test_env.step(action)
                    total_reward += reward
                    state = next_state
            avg_reward = total_reward / n_episodes
            if self.writer:
                self.writer.add_scalar('dyna/test_episode_reward', avg_reward, self.global_step)

    def plan(self, simulator_steps):
        if self.global_step < self.warmup_period:
            return
        query_list = []
        for _ in range(simulator_steps):
            if self.queries:
                query_list.append(self.queries.pop_max())
        if query_list:
            trajectories = self.rollout(query_list)
            batch = Experience(
                trajectories.state,
                trajectories.action,
                trajectories.inner_return,
                trajectories.final_state,
                trajectories.done
            )
            td_errors = self.update_agent(batch, trajectories.n)
            cpu_batch = list(map(lambda x: x.detach().cpu().numpy(), batch))
            experiences = list(map(lambda x: Experience(*x), zip(*cpu_batch)))
            for experience, n, td_error in zip(experiences, trajectories.n, td_errors):
                self.queue_rollouts(experience, n.detach().cpu().numpy(), td_error)

    def queue_rollouts(self, experience, n, td_error):
        state, _, inner_return, final_state, done = experience
        priority = np.abs(td_error) * self.priority_decay**n
        if priority > self.priority_threshold:
            if self.max_rollout_length is None or (n < self.max_rollout_length):
                for action in np.arange(self.action_space.n):
                    new_query = Trajectory(action, state, inner_return, final_state, done, n)
                    self.queries.push(new_query, priority)

    def rollout(self, batch):
        batch = Trajectory(*list(zip(*batch)))
        batch = self._torchify_planning_query(batch)
        new_state, new_reward = self.model(batch.state, batch.action)
        new_return = new_reward.detach().squeeze(-1) + self.gamma * batch.inner_return
        simulated_experiences = Trajectory(
            batch.action,
            new_state.detach(),
            new_return,
            batch.final_state,
            batch.done,
            batch.n
        )
        return simulated_experiences

    def update_agent(self, batch, n_steps=0):
        q_predictions = self.critic(batch.state)
        q_acted = q_predictions.gather(dim=1, index=batch.action.long()).squeeze(1)
        with torch.no_grad():
            next_action = torch.argmax(self.critic(batch.next_state), dim=-1)
            next_q_values = self.critic_target(batch.next_state)
            boostrapped_value = next_q_values.gather(dim=1, index=next_action.long().unsqueeze(1)).squeeze(1)
            discounted_value = (1 - batch.done) * self.gamma**n_steps * boostrapped_value
            q_label = batch.reward + discounted_value
            td_error = q_label - q_acted
        loss = torch.nn.functional.smooth_l1_loss(input=q_acted, target=q_label)
        self.critic_optimizer.zero_grad()
        loss.backward()
        self.critic_optimizer.step()
        if self.writer:
            self.writer.add_scalar('dyna/critic_loss', loss.detach(), self.global_step)
            self.writer.add_scalar('dyna/mean_abs_td_error', td_error.abs().mean(), self.global_step)
            self.writer.add_scalar('dyna/mean_q_acted', q_acted.detach().mean(), self.global_step)
            self.writer.add_scalar('dyna/mean_q_label', q_label.detach().mean(), self.global_step)
            self.writer.add_histogram('dyna/q_acted', q_acted.detach(), self.global_step)
            self.writer.add_histogram('dyna/q_label', q_label, self.global_step)
            self.writer.add_histogram('dyna/td_error', td_error, self.global_step)
        self.global_step += 1
        if self.global_step >= self.warmup_period:
            self.epsilon = max(self.epsilon*self.epsilon_decay_rate, self.final_epsilon_value)
        soft_update(self.critic_target, self.critic, tau=self.target_moving_average)
        return td_error.detach().cpu().numpy()

    def update_model(self, batch):
        state, action, reward, next_state, _ = batch
        prev_state, prev_reward = self.model(next_state, action)
        state_loss = torch.nn.functional.smooth_l1_loss(input=prev_state, target=state)
        reward_loss = torch.nn.functional.smooth_l1_loss(input=prev_reward, target=reward,)
        loss = state_loss + reward_loss
        self.model_optimizer.zero_grad()
        loss.backward()
        self.model_optimizer.step()
        if self.writer:
            self.writer.add_scalar('dyna/model_loss', loss.detach(), self.global_step)
            self.writer.add_scalar('dyna/model_state_loss', state_loss.detach(), self.global_step)
            self.writer.add_scalar('dyna/model_state_abs_err', (prev_state.detach()-state).abs().mean(), self.global_step)
            self.writer.add_scalar('dyna/model_reward_loss', reward_loss.detach(), self.global_step)
        return loss.detach()

    def _torchify_experience(self, batch):
        batch = map(np.stack, batch)
        states, actions, rewards, next_states, dones = batch
        states = torch.from_numpy(states).float().to(self.device)
        actions = torch.from_numpy(actions).long().to(self.device).unsqueeze(-1)
        rewards = torch.from_numpy(rewards).float().to(self.device)
        next_states = torch.from_numpy(next_states).float().to(self.device)
        dones = torch.from_numpy(dones).byte().to(self.device)
        batch = Experience(states, actions, rewards, next_states, dones)
        return batch

    def _torchify_planning_query(self, batch):
        batch = list(map(np.stack, batch))
        action, state, inner_return, final_state, done, n_steps = batch
        action = torch.from_numpy(action).long().to(self.device).unsqueeze(-1)
        state = torch.from_numpy(state).float().to(self.device)
        inner_return = torch.from_numpy(inner_return).float().to(self.device)
        final_state = torch.from_numpy(final_state).float().to(self.device)
        done = torch.from_numpy(done).byte().to(self.device)
        n_steps = torch.from_numpy(n_steps).long().to(self.device)
        batch = Trajectory(action, state, inner_return, final_state, done, n_steps)
        return batch

def train_agent(args):
    env = gym.make(args.env)
    test_env = gym.make(args.env)
    agent = DynaAgent(env.observation_space, env.action_space, args.gamma, args)

    state = env.reset()
    episode = 0
    ep_reward = 0
    for _ in range(args.iterations):
        experiences = []
        for _ in range(args.interactions_per_iter):
            action = agent.act(state)
            next_state, reward, done, _ = env.step(action)
            ep_reward += reward
            experiences.append(Experience(state, action, reward, next_state, done))
            state = next_state if not done else env.reset()
            if done:
                episode+=1
                print(episode, ep_reward)
                if agent.writer:
                    agent.writer.add_scalar('dyna/episode', episode, agent.global_step)
                    agent.writer.add_scalar('dyna/train_episode_reward', ep_reward, agent.global_step)

                ep_reward = 0
        agent.train(experiences)
        agent.plan(simulator_steps=args.planning_steps_per_iter)
        agent.test(test_env, args.episodes_per_eval)

if __name__ == "__main__":
    args = model_based_parser.parse_args()
    if args.gpu and torch.cuda.is_available():
        args.device = torch.device('cuda')
    else:
        args.device = torch.device('cpu')
    train_agent(args)
