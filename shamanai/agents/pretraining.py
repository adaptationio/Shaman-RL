# -*- coding: utf-8 -*-
"""pretraining.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/github/Stable-Baselines-Team/rl-colab-notebooks/blob/sb3/pretraining.ipynb

## Stable Baselines3 - Pretraining with Behavior Cloning


Github repo: https://github.com/araffin/rl-tutorial-jnrr19

Stable-Baselines3: https://github.com/DLR-RM/stable-baselines3

Documentation: https://stable-baselines.readthedocs.io/en/master/

RL Baselines3 zoo: https://github.com/DLR-RM/rl-baselines3-zoo


[RL Baselines3 Zoo](https://github.com/DLR-RM/rl-baselines3-zoo) is a collection of pre-trained Reinforcement Learning agents using Stable-Baselines3.

It also provides basic scripts for training, evaluating agents, tuning hyperparameters and recording videos.


## Introduction

In this notebook, you will learn how to record expert data, then pre-train an agent using this data
and finally continue training with Stable-Baselines3.


## Install Dependencies and Stable Baselines3 Using Pip

List of full dependencies can be found in the [README](https://github.com/DLR-RM/stable-baselines3).

Notebook originally created by [skervim](https://github.com/skervim)
"""

# For Box2D env
!apt-get install swig
!pip install gym[box2d]
!pip install stable-baselines3[extra]

import gym
from tqdm import tqdm
import numpy as np

import torch as th
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR

from stable_baselines3 import PPO, A2C, SAC, TD3
from stable_baselines3.common.evaluation import evaluate_policy

# Example for continuous actions
# env_id = "LunarLanderContinuous-v2"

# Example for discrete actions
env_id = "CartPole-v1"

env = gym.make(env_id)

"""## Train Expert Model

We create an expert RL agent and let it learn to solve a task by interacting with the evironment.

"""

ppo_expert = PPO('MlpPolicy', env_id, verbose=1, create_eval_env=True)
ppo_expert.learn(total_timesteps=3e4, eval_freq=10000)
ppo_expert.save("ppo_expert")

"""check the performance of the trained agent"""

mean_reward, std_reward = evaluate_policy(ppo_expert, env, n_eval_episodes=10)

print(f"Mean reward = {mean_reward} +/- {std_reward}")

"""## Create Student

We also create a student RL agent, which will later be trained with the expert dataset

"""

a2c_student = A2C('MlpPolicy', env_id, verbose=1)

# only valid for continuous actions
# sac_student = SAC('MlpPolicy', env_id, verbose=1, policy_kwargs=dict(net_arch=[64, 64]))

"""
We now let our expert interact with the environment (except we already have expert data) and store resultant expert observations and actions to build an expert dataset.
"""

num_interactions = int(4e4)

if isinstance(env.action_space, gym.spaces.Box):
  expert_observations = np.empty((num_interactions,) + env.observation_space.shape)
  expert_actions = np.empty((num_interactions,) + (env.action_space.shape[0],))

else:
  expert_observations = np.empty((num_interactions,) + env.observation_space.shape)
  expert_actions = np.empty((num_interactions,) + env.action_space.shape)

obs = env.reset()

for i in tqdm(range(num_interactions)):
    action, _ = ppo_expert.predict(obs, deterministic=True)
    expert_observations[i] = obs
    expert_actions[i] = action
    obs, reward, done, info = env.step(action)
    if done:
        obs = env.reset()

np.savez_compressed(
    "expert_data",
    expert_actions=expert_actions,
    expert_observations=expert_observations,
)

"""

- To seamlessly use PyTorch in the training process, we subclass an `ExpertDataset` from PyTorch's base `Dataset`.
- Note that we initialize the dataset with the previously generated expert observations and actions.
- We further implement Python's `__getitem__` and `__len__` magic functions to allow PyTorch's dataset-handling to access arbitrary rows in the dataset and inform it about the length of the dataset.
- For more information about PyTorch's datasets, you can read: https://pytorch.org/docs/stable/data.html.
"""

from torch.utils.data.dataset import Dataset, random_split

class ExpertDataSet(Dataset):
    def __init__(self, expert_observations, expert_actions):
        self.observations = expert_observations
        self.actions = expert_actions
        
    def __getitem__(self, index):
        return (self.observations[index], self.actions[index])

    def __len__(self):
        return len(self.observations)

"""

We now instantiate the `ExpertDataSet` and split it into training and test datasets.
"""

expert_dataset = ExpertDataSet(expert_observations, expert_actions)

train_size = int(0.8 * len(expert_dataset))

test_size = len(expert_dataset) - train_size

train_expert_dataset, test_expert_dataset = random_split(
    expert_dataset, [train_size, test_size]
)

print("test_expert_dataset: ", len(test_expert_dataset))
print("train_expert_dataset: ", len(train_expert_dataset))

"""

NOTE: The supervised learning section of this code is adapted from: https://github.com/pytorch/examples/blob/master/mnist/main.py
1. We extract the policy network of our RL student agent.
2. We load the (labeled) expert dataset containing expert observations as inputs and expert actions as targets.
3. We perform supervised learning, that is, we adjust the policy network's parameters such that given expert observations as inputs to the network, its outputs match the targets (expert actions).
By training the policy network in this way the corresponding RL student agent is taught to behave like the expert agent that was used to created the expert dataset (Behavior Cloning).
"""

def pretrain_agent(
    student,
    batch_size=64,
    epochs=1000,
    scheduler_gamma=0.7,
    learning_rate=1.0,
    log_interval=100,
    no_cuda=True,
    seed=1,
    test_batch_size=64,
):
    use_cuda = not no_cuda and th.cuda.is_available()
    th.manual_seed(seed)
    device = th.device("cuda" if use_cuda else "cpu")
    kwargs = {"num_workers": 1, "pin_memory": True} if use_cuda else {}

    if isinstance(env.action_space, gym.spaces.Box):
      criterion = nn.MSELoss()
    else:
      criterion = nn.CrossEntropyLoss()

    # Extract initial policy
    model = student.policy.to(device)

    def train(model, device, train_loader, optimizer):
        model.train()

        for batch_idx, (data, target) in enumerate(train_loader):
            data, target = data.to(device), target.to(device)
            optimizer.zero_grad()

            if isinstance(env.action_space, gym.spaces.Box):
              # A2C/PPO policy outputs actions, values, log_prob
              # SAC/TD3 policy outputs actions only
              if isinstance(student, (A2C, PPO)):
                action, _, _ = model(data)
              else:
                # SAC/TD3:
                action = model(data)
              action_prediction = action.double()
            else:
              # Retrieve the logits for A2C/PPO when using discrete actions
              latent_pi, _, _ = model._get_latent(data)
              logits = model.action_net(latent_pi)
              action_prediction = logits
              target = target.long()

            loss = criterion(action_prediction, target)
            loss.backward()
            optimizer.step()
            if batch_idx % log_interval == 0:
                print(
                    "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
                        epoch,
                        batch_idx * len(data),
                        len(train_loader.dataset),
                        100.0 * batch_idx / len(train_loader),
                        loss.item(),
                    )
                )
    def test(model, device, test_loader):
        model.eval()
        test_loss = 0
        with th.no_grad():
            for data, target in test_loader:
                data, target = data.to(device), target.to(device)

                if isinstance(env.action_space, gym.spaces.Box):
                  # A2C/PPO policy outputs actions, values, log_prob
                  # SAC/TD3 policy outputs actions only
                  if isinstance(student, (A2C, PPO)):
                    action, _, _ = model(data)
                  else:
                    # SAC/TD3:
                    action = model(data)
                  action_prediction = action.double()
                else:
                  # Retrieve the logits for A2C/PPO when using discrete actions
                  latent_pi, _, _ = model._get_latent(data)
                  logits = model.action_net(latent_pi)
                  action_prediction = logits
                  target = target.long()

                test_loss = criterion(action_prediction, target)
        test_loss /= len(test_loader.dataset)
        print(f"Test set: Average loss: {test_loss:.4f}")

    # Here, we use PyTorch `DataLoader` to our load previously created `ExpertDataset` for training
    # and testing
    train_loader = th.utils.data.DataLoader(
        dataset=train_expert_dataset, batch_size=batch_size, shuffle=True, **kwargs
    )
    test_loader = th.utils.data.DataLoader(
        dataset=test_expert_dataset, batch_size=test_batch_size, shuffle=True, **kwargs,
    )

    # Define an Optimizer and a learning rate schedule.
    optimizer = optim.Adadelta(model.parameters(), lr=learning_rate)
    scheduler = StepLR(optimizer, step_size=1, gamma=scheduler_gamma)

    # Now we are finally ready to train the policy model.
    for epoch in range(1, epochs + 1):
        train(model, device, train_loader, optimizer)
        test(model, device, test_loader)
        scheduler.step()

    # Implant the trained policy network back into the RL student agent
    a2c_student.policy = model

"""Evaluate the agent before pretraining, it should be random"""

mean_reward, std_reward = evaluate_policy(a2c_student, env, n_eval_episodes=10)

print(f"Mean reward = {mean_reward} +/- {std_reward}")

"""

Having defined the training procedure we can now run the pretraining!
"""

pretrain_agent(
    a2c_student,
    epochs=3,
    scheduler_gamma=0.7,
    learning_rate=1.0,
    log_interval=100,
    no_cuda=True,
    seed=1,
    batch_size=64,
    test_batch_size=1000,
)
a2c_student.save("a2c_student")

"""

Finally, let us test how well our RL agent student learned to mimic the behavior of the expert
"""

mean_reward, std_reward = evaluate_policy(a2c_student, env, n_eval_episodes=10)

print(f"Mean reward = {mean_reward} +/- {std_reward}")