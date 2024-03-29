import math
import time
import datetime

import gym

import torch
import torchvision

from torchvision import datasets
from torchvision import transforms

from torch.autograd import Function
from torch.autograd import Variable

from torch import optim
from torch.optim.lr_scheduler import StepLR
from torch.optim.optimizer import Optimizer, required

from torch import nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.nn import Module
from torch.nn import init

from torch.distributions import Normal, Categorical

!pip install git+https://github.com/activatedgeek/kondo.git@master -q
!pip install https://github.com/activatedgeek/torchrl/tarball/master -q

from kondo import Spec
from kondo import Experiment
from kondo import HParams

import torchrl
from torchrl.experiments import BaseExperiment

from torchrl.contrib import controllers

from torchrl.utils.storage import TransitionTupleDataset
from torchrl.utils import ExpDecaySchedule

DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

"""##Weight transport

###Слои
"""

class LinearFunction_FA(Function):
    @staticmethod
    def forward(ctx, input, weight, back_weight, bias=None):
        ctx.save_for_backward(input, weight, back_weight, bias)
        output = input.mm(weight.t())
        if bias is not None:
            output += bias.unsqueeze(0).expand_as(output)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input, weight, back_weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_back_weight = grad_bias = None

        if ctx.needs_input_grad[0]:
            grad_input = grad_output.mm(back_weight.t())
        if ctx.needs_input_grad[1]:
            grad_weight = grad_output.t().mm(input)
        if bias is not None and ctx.needs_input_grad[3]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_weight, grad_back_weight, grad_bias



class LinearFunction_KP(LinearFunction_FA):
    @staticmethod
    def backward(ctx, grad_output):
        assert ctx.needs_input_grad[1] or not ctx.needs_input_grad[2],\
        "Cant compute backward weight gradient without forward weight"

        input, weight, back_weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_back_weight = grad_bias = None
        
        if ctx.needs_input_grad[0]:
            grad_input = grad_output.mm(back_weight.t())
        if ctx.needs_input_grad[1]:
            grad_weight = grad_output.t().mm(input)
        if ctx.needs_input_grad[2]:
            grad_back_weight = grad_weight.t()
        if bias is not None and ctx.needs_input_grad[3]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_weight, grad_back_weight, grad_bias

class Linear_FA(Module):
    def __init__(self, in_features, out_features, bias=True, device=DEVICE):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.weight = Parameter(torch.Tensor(out_features, in_features).to(device))
        self.back_weight = Parameter(torch.Tensor(in_features, out_features).to(device))
        if bias:
            self.bias = Parameter(torch.Tensor(out_features)).to(device)
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        init.kaiming_uniform_(self.back_weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        return LinearFunction_FA.apply(input, self.weight, self.back_weight, self.bias)



class Linear_KP(Linear_FA):
    def forward(self, input):
        return LinearFunction_KP.apply(input, self.weight, self.back_weight, self.bias)


class Linear_WM(Linear_FA):
    @torch.no_grad()
    def mirror(self, n, activation=None, mirror_lr=1e-3):
        for i in range(n):
            noise_x = torch.normal(0, 1, size=(1,self.in_features), device=DEVICE)
            if activation is not None:
                noise_y = activation(self.forward(noise_x))
            else:
                noise_y = self.forward(noise_x)
            self.back_weight.add_(noise_x.t().mm(noise_y), alpha=mirror_lr)

        # Prevent feedback weights growing too large
        x = torch.rand((self.in_features, n), device=DEVICE)
        y = self.back_weight.t().mm(x)
        y_std = y.std().mean()
        self.back_weight.div_(y_std.div(2))

"""##RL

###DQN
"""

def mirror_seq(net, n=1):
    for m in net.modules():
        if isinstance(m, Linear_WM):
            m.mirror(n)

class QNet(nn.Module):
    def __init__(self, input_size, output_size, net_type):
        super(QNet, self).__init__()

        self._input_size = input_size
        self._output_size = output_size
        self._hidden_size = 128
        assert net_type in ['BP', 'FA', 'KP', 'WM'],\
                        'Network type must be in BP, FA, KP, WM'

        if net_type == 'BP':   layer = nn.Linear
        elif net_type == 'FA': layer = Linear_FA
        elif net_type == 'KP': layer = Linear_KP
        elif net_type == 'WM': layer = Linear_WM

        self.net = nn.Sequential(
        layer(self._input_size, self._hidden_size),
        nn.ReLU(),
        layer(self._hidden_size, self._hidden_size),
        nn.ReLU(),
        layer(self._hidden_size, self._output_size)
        )

    def mirror(self, n=1):
        return mirror_seq(self.net, n)
    def forward(self, obs):
        return self.net.forward(obs)

class DQNController(controllers.DQNController):
    def __init__(self, obs_size, action_size, _controller_args,
                 device, net_type):
        super().__init__(obs_size, action_size, **_controller_args, device=device)
        self.q_net = QNet(obs_size, action_size, net_type).to(device)
        self.target_q_net = QNet(obs_size, action_size, net_type).to(device)
        self.target_q_net.load_state_dict(self.q_net.state_dict())

        self.q_net_optim = torch.optim.Adam(self.q_net.parameters(), 
                                            lr=_controller_args['lr'])

class DQNExperiment(BaseExperiment):
  def __init__(self, double_dqn=False, gamma=.99,
               batch_size=32, lr=1e-3, buffer_size=1000, eps_max=1.0,
               eps_min=1e-2, n_eps_anneal=100, n_update_interval=10,
               net_type='BP', **kwargs):
    self.net_type = net_type
    self._controller_args = dict(
        double_dqn=double_dqn,
        gamma=gamma,
        lr=lr,
        eps_max=eps_max,
        eps_min=eps_min,
        n_eps_anneal=n_eps_anneal,
        n_update_interval=n_update_interval,
    )

    self.buffer = TransitionTupleDataset(size=buffer_size)
    self.batch_size = batch_size

    super().__init__(**kwargs)

  def store(self, transition_list):
    self.buffer.extend(transition_list)

  def build_controller(self):
    return DQNController(self.envs.observation_space.shape[0], 
                         self.envs.action_space.n,
                         self._controller_args,
                         device=self.device,
                         net_type=self.net_type)

  def train(self):
    if len(self.buffer) < self.batch_size:
        return {}

    b_idx = torch.randperm(len(self.buffer))[:self.batch_size]
    b_transition = [b.to(self.device) for b in self.buffer[b_idx]]
    self.controller.q_net.mirror()
    return self.controller.learn(*b_transition)

  @staticmethod
  def spec_list():
    return [
        Spec(
            group='dqn',
            params=dict(
                env_id=['CartPole-v0'],
                net_type=['BP', 'FA', 'KP', 'WM'],
                gamma=.99,
                n_train_interval=1,
                n_frames=50000,
                n_envs=10,
                batch_size=256,
                buffer_size=5000,
                double_dqn=False,
                eps_max=1.0,
                eps_min=1e-2,
                n_update_interval=10,
                lr=1e-3,
                n_eps_anneal=500,
            ),
            exhaustive=True
        )]

"""###A2C

"""

class A2CNet(nn.Module):
    def __init__(self, input_size, output_size, hidden_size, net_type, *args):
        super(A2CNet, self).__init__()
        print(args)
        self._input_size = input_size
        self._output_size = output_size
        self._hidden_size = hidden_size

        if net_type == 'BP':   layer = nn.Linear
        elif net_type == 'FA': layer = Linear_FA
        elif net_type == 'KP': layer = Linear_KP
        elif net_type == 'WM': layer = Linear_WM

        self.critic = nn.Sequential(
            layer(self._input_size, self._hidden_size),
            nn.ReLU(),
            layer(self._hidden_size, 1)
        )

        self.actor = nn.Sequential(
            layer(self._input_size, self._hidden_size),
            nn.ReLU(),
            layer(self._hidden_size, self._output_size),
            nn.Softmax(dim=1)
        )

    def forward(self, obs):
        value = self.critic(obs)
        policy = self.actor(obs)
        dist = Categorical(policy)
        return value, dist

class A2CController(controllers.A2CController):
    def __init__(self, obs_size, action_size, net_type,
                 _controller_args, device=None):
        super().__init__(obs_size, action_size, **_controller_args, device=device)
        print(obs_size, action_size, 256, net_type)
        self.ac_net = A2CNet(obs_size, action_size, 256, net_type).to(self.device)
        self.ac_net_optim = torch.optim.Adam(self.ac_net.parameters(), lr=_controller_args['lr'])

class A2CExperiment(BaseExperiment):
  def __init__(self, gamma=0.99, rollout_steps=5, alpha=0.5,
               lr=3e-4, beta=1e-3, lmbda=1.0, net_type='BP', **kwargs):
    self.net_type = net_type
    self._controller_args = dict(
        lr=lr,
        gamma=gamma,
        lmbda=lmbda,
        alpha=alpha,
        beta=beta
    )

    kwargs['n_train_interval'] = kwargs.get('n_envs', 1) * rollout_steps

    super().__init__(**kwargs)

    self.buffers = [TransitionTupleDataset()
                    for _ in range(self.envs.n_procs)]

  def store(self, transition_list):
    for buffer, transition in zip(self.buffers, transition_list):
      buffer.extend([transition])

  def build_controller(self):
    return A2CController(self.envs.observation_space.shape[0],
                         self.envs.action_space.n,
                         self.net_type,
                         self._controller_args,
                         device=self.device)

  def train(self):
    all_transitions = [[], [], [], [], []]
    all_returns = []

    for buffer in self.buffers:
      batch = [b.to(self.device) for b in buffer[:]]
      r = self.controller.compute_return(*batch)
      all_returns.append(r)

      for i, b in enumerate(batch):
        all_transitions[i].append(b)

      buffer.truncate()

    all_transitions = [torch.cat(t, dim=0) for t in all_transitions]
    all_returns = torch.cat(all_returns, dim=0)

    return self.controller.learn(*all_transitions, all_returns)

  @staticmethod
  def spec_list():
    return [
        Spec(
            group='a2c',
            exhaustive=True,
            params=dict(
                env_id=['CartPole-v0'],
                net_type=['BP', 'FA', 'KP', 'WM'],
                n_envs=16,
                n_frames=int(5e5),
                rollout_steps=5,
                gamma=0.99,
                lmbda=1.0,
                alpha=0.5,
                beta=1e-3,
                lr=3e-4
            )
        )
    ]

"""##Тесты

###RL
"""

# Commented out IPython magic to ensure Python compatibility.
# %load_ext tensorboard

# Commented out IPython magic to ensure Python compatibility.
# %tensorboard --logdir logs

import shutil
shutil.rmtree('/content/logs')

"""###Mnist"""

# Device configuration
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Hyper-parameters 
input_size = 784
hidden_size = 500
num_classes = 10
num_epochs = 3
batch_size = 100
learning_rate = 0.001

class NeuralNet(nn.Module):
    def __init__(self, input_size, hidden_size, out_size, net_type):
        super().__init__()
        if net_type == 'BP':   layer = nn.Linear
        elif net_type == 'FA': layer = Linear_FA
        elif net_type == 'KP': layer = Linear_KP
        elif net_type == 'WM': layer = Linear_WM
        self.fc1 = layer(input_size, hidden_size).to(DEVICE)
        self.relu1 = nn.ReLU()
        self.fc2 = layer(hidden_size, hidden_size).to(DEVICE)
        self.relu2 = nn.ReLU()
        self.fc3 = layer(hidden_size, out_size).to(DEVICE)
        self.sigmoid = nn.Sigmoid()

        self.net_type = net_type
    
    def forward(self, x):
        out = self.fc1(x)
        out = self.relu1(out)
        out = self.fc2(out)
        out = self.relu2(out)
        out = self.fc3(out)
        out = self.sigmoid(out)
        return out
    
    def mirror(self, n=1):
        if self.net_type == 'WM':
            for m in self.modules():
                if isinstance(m, Linear_WM):
                    m.mirror(n)

class MNISTExperiment(Experiment):
    def __init__(self, net_type='BP', hidden_size=None,
                 n_epochs=25, optimizer='SGD', **kwargs):
        super().__init__(**kwargs)

        self.net = NeuralNet(784, hidden_size, 10, net_type)

        if optimizer=='SGD':
            self.opt = optim.SGD(self.net.parameters(), lr=1e-3)
        elif optimizer=='Adam':
            self.opt = optim.Adam(self.net.parameters(), lr=1e-3)
        else:
            raise NotImplementedError

        self.optimizer_name = optimizer
        self.loss_fn = nn.CrossEntropyLoss()

        self.n_epochs = n_epochs
        self.net_type = net_type
    
    def run(self):
        total_step = len(train_loader)
        step = 0
        for epoch in range(self.n_epochs):
            with torch.no_grad():
                correct = 0
                total = 0
                for images, labels in test_loader:
                    images = images.reshape(-1, 28*28).to(DEVICE)
                    labels = labels.to(DEVICE)
                    outputs = self.net(images)
                    _, predicted = torch.max(outputs.data, 1)
                    total += labels.size(0)
                    correct += (predicted == labels).sum().item()
                self.logger.add_scalar('Test/acc', correct/total, step)

            for i, (images, labels) in enumerate(train_loader):  
                # Move tensors to the configured device
                images = images.reshape(-1, 28*28).to(DEVICE)
                labels = labels.to(DEVICE)
                
                # Forward pass
                outputs = self.net(images)
                
                # Backward and optimize
                self.opt.zero_grad()
                loss = self.loss_fn(outputs, labels)
                loss.backward()
                self.opt.step()
                self.net.mirror() # <- костыль, но покатит
                
                self.logger.add_scalar('Train/loss', loss.item(), step)
                step += 1

        with torch.no_grad():
            correct = 0
            total = 0
            for images, labels in test_loader:
                images = images.reshape(-1, 28*28).to(DEVICE)
                labels = labels.to(DEVICE)
                outputs = self.net(images)
                _, predicted = torch.max(outputs.data, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()
            self.logger.add_scalar('Test/acc', correct/total, step)
                

    @staticmethod
    def spec_list():
        return [
            Spec(
                params=dict(
                    net_type=['BP', 'FA', 'KP', 'WM'],
                    optimizer='Adam',
                    hidden_size=128,
                ),
                exhaustive=True
            )]

# Commented out IPython magic to ensure Python compatibility.
# %tensorboard --logdir logs

!zip -r ./logs.zip ./logs
from google.colab import files
files.download("./logs.zip")
#import shutil
#shutil.rmtree('/content/logs')

# MNIST dataset
train_dataset = torchvision.datasets.MNIST(root='../../data', 
                                           train=True, 
                                           transform=transforms.ToTensor(),  
                                           download=True)

test_dataset = torchvision.datasets.MNIST(root='../../data', 
                                          train=False, 
                                          transform=transforms.ToTensor())

train_loader = torch.utils.data.DataLoader(dataset=train_dataset, 
                                           batch_size=batch_size, 
                                           shuffle=True)

test_loader = torch.utils.data.DataLoader(dataset=test_dataset, 
                                          batch_size=batch_size, 
                                          shuffle=False)

for i in range(5):
    hparams = HParams(MNISTExperiment)
    for name, trial in hparams.trials():
        MNISTExperiment(**trial, log_dir=f"./logs/MNIST/{trial['net_type']}_{name}").run()

for i in range(5):
    hparams = HParams(DQNExperiment)
    for name, trial in hparams.trials():
        log_dir = './logs/DQN/' + trial['net_type'] + name
        print(log_dir, trial)
        exp = DQNExperiment(**trial, log_dir=log_dir)
        exp.run()

for i in range(5):
    hparams = HParams(A2CExperiment)
    for name, trial in hparams.trials():
        log_dir = './logs/A2C/' + trial['net_type'] + name
        print(log_dir, trial)
        exp = A2CExperiment(**trial, log_dir=log_dir)
        exp.run()

!zip -r ./logs.zip ./logs
from google.colab import files
files.download("./logs.zip")

"""# New Section"""
