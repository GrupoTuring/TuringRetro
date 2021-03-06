import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F

from .experience_replay import ReplayBuffer, NStepBuffer
from .dqn import Network

class DQNAgent:
    """
    Uma classe que cria um agente DQN que utiliza NStepBuffer como memória
    """
    def __init__(self, 
                 observation_space, 
                 action_space,
                 batch_size=32,
                 max_memory=100000,
                 n_step=3,
                 alpha = 0.6,
                 beta = 0.4,
                 beta_decay = 2e-5, 
                 lr=7e-4, 
                 gamma=0.99,
                 tau=0.01,
                 epsilon_init=0.5,
                 epsilon_decay=0.9995,
                 min_epsilon=0.01):
        """
        Inicializa o agente com os parâmetros dados
        """
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.batch_size = batch_size
        self.alpha = alpha
        self.loss_param = beta
        self.loss_param_decay = beta_decay
        self.gamma = gamma
        self.tau = tau
        self.n_step = n_step
        self.memory = ReplayBuffer(max_memory, observation_space.shape, alpha, 1e-4)
        self.n_step_buffer = NStepBuffer(observation_space.shape, gamma, n_step)
        self.action_space = action_space

        self.epsilon = epsilon_init
        self.epsilon_decay = epsilon_decay
        self.min_epsilon = min_epsilon

        self.dqn = Network(observation_space.shape, action_space.n).to(self.device)
        self.target_dqn = Network(observation_space.shape, action_space.n).to(self.device)
        self.target_dqn.eval()

        for target_param, param in zip(self.dqn.parameters(),self.target_dqn.parameters()):
            target_param.data.copy_(param)

        self.optimizer  = optim.Adam(self.dqn.parameters(), lr=lr)

    def act(self, state, evaluate=False):
        self.epsilon *= self.epsilon_decay
        self.epsilon = max(self.epsilon, self.min_epsilon)

        if np.random.random() < self.epsilon and not evaluate:
            action = self.action_space.sample()
            return action

        with torch.no_grad():
            state = torch.FloatTensor(state).unsqueeze(0).to(self.device, non_blocking=True)
            action = self.dqn.forward(state).argmax(dim=-1)
            action = action.cpu().numpy()[0]

        return action

    def remember(self, state, action, reward, new_state, done):
        experience = self.n_step_buffer.update(state, action, reward, new_state, done)
        if(experience != None):
            self.memory.update(experience[0],experience[1],experience[2],experience[3],experience[4])
        

    def train(self, epochs=1):
        # Se temos menos experiências que o batch size
        # não começamos o treinamento
        if self.batch_size > self.memory.size:
            return -float("inf")
        
        for epoch in range(epochs):
            # Pegamos uma amostra das nossas experiências para treinamento
            (states, actions, rewards, next_states, dones, priorities, indexes) = self.memory.sample(self.batch_size)

            # Transformar nossas experiências em tensores
            states = torch.as_tensor(states).to(self.device, non_blocking=True)
            actions = torch.as_tensor(actions).to(self.device, non_blocking=True).unsqueeze(-1)
            rewards = torch.as_tensor(rewards).to(self.device, non_blocking=True).unsqueeze(-1)
            next_states = torch.as_tensor(next_states).to(self.device, non_blocking=True)
            dones = torch.as_tensor(dones).to(self.device, non_blocking=True).unsqueeze(-1)

            curr_q = self.dqn.forward(states).gather(-1, actions.long())

            with torch.no_grad():
                next_q = self.target_dqn.forward(next_states)
                max_next_q = torch.max(next_q, -1)[0]
                max_next_q = max_next_q.view(max_next_q.size(0), 1)

                target = (rewards + (1 - dones) * (self.gamma ** self.n_step) * max_next_q).to(self.device)
                N = len(self.memory)
                w = (N * priorities) ** (-self.loss_param)
                w = w/w.max()
                self.loss_param *= 1+self.loss_param_decay if self.loss_param < 1 else 1

            loss = F.mse_loss(curr_q, target, reduction="none")
            self.memory.update_priority(indexes, torch.abs(loss))

            w = torch.as_tensor(w).to(self.device).unsqueeze(-1)
            weighted_loss = loss * w
            final_loss = torch.mean(weighted_loss)

            self.optimizer.zero_grad()
            final_loss.backward()
            for param in self.dqn.parameters():
                param.grad.data.clamp_(-100,100)
            self.optimizer.step()

            self.update_target()

            return final_loss

    def save_model(self, path):
        torch.save(self.dqn.state_dict(), path)
    
    def load_model(self, path):
        self.dqn.load_state_dict(torch.load(path))
        self.target_dqn.load_state_dict(torch.load(path))

    def update_target(self):
        with torch.no_grad():
            for target_param, param in zip(self.target_dqn.parameters(), self.dqn.parameters()):
                target_param.data.mul_(1 - self.tau)
                torch.add(target_param.data, param.data, alpha=self.tau, out=target_param.data)