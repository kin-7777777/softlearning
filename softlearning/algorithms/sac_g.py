from copy import deepcopy
from collections import OrderedDict
from numbers import Number

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from softlearning.utils.gym import is_continuous_space, is_discrete_space
from .rl_algorithm import RLAlgorithm


import os
import copy
import torch
import tree # for flattening batch OrderedDict objects for gamma

from gamma.flows import (
    make_conditional_flow,
    save_model,
    load_model,
)
from gamma.td.distributions import BootstrapTarget
from gamma.td.structs import (
    ReplayPool,
    Policy,
)
from gamma.td.utils import (
    soft_update_from_to,
    format_batch_sac, # custom version
)
from gamma.utils import (
    mkdir,
    set_device,
)

@tf.function(experimental_relax_shapes=True)
def td_targets(rewards, discounts, next_values):
    return rewards + discounts * next_values


@tf.function(experimental_relax_shapes=True)
def compute_Q_targets(next_Q_values,
                      next_log_pis,
                      rewards,
                      terminals,
                      discount,
                      entropy_scale,
                      reward_scale):
    next_values = next_Q_values - entropy_scale * next_log_pis
    terminals = tf.cast(terminals, next_values.dtype)

    Q_targets = td_targets(
        rewards=reward_scale * rewards,
        discounts=discount,
        next_values=(1.0 - terminals) * next_values)

    return Q_targets


def heuristic_target_entropy(action_space):
    if is_continuous_space(action_space):
        heuristic_target_entropy = -np.prod(action_space.shape)
    elif is_discrete_space(action_space):
        raise NotImplementedError(
            "TODO(hartikainen): implement for discrete spaces.")
    else:
        raise NotImplementedError((type(action_space), action_space))

    return heuristic_target_entropy

# From gamma-models
class Args:
    ## paths
    data_path = '../data/pools/pendulum.pkl'
    policy_path = '../data/policies/pendulum.pkl'
    save_path = '../logs/pendulum'
    
    load_epoch = None
    device = 'cuda:0'
    
    ## model
    hidden_dims = [256, 256, 256]
    sigma = 0.1
    
    ## training
    batch_size = 1024
    lr = 1e-4
    decay = 1e-5
    tau = 0.005
    discount = 0.99
    sample_discount = 0.9
    burnin_discount = 0.5
    
    n_burnin = 2000
    n_steps = 100000
    
    vis_freq = 100
    save_freq = 1000


class SAC_G(RLAlgorithm):
    """Soft Actor-Critic with MVE Gamma (SAC)

    References
    ----------
    [1] Tuomas Haarnoja*, Aurick Zhou*, Kristian Hartikainen*, George Tucker,
        Sehoon Ha, Jie Tan, Vikash Kumar, Henry Zhu, Abhishek Gupta, Pieter
        Abbeel, and Sergey Levine. Soft Actor-Critic Algorithms and
        Applications. arXiv preprint arXiv:1812.05905. 2018.
    """

    def __init__(
            self,
            training_environment,
            evaluation_environment,
            policy,
            Qs,
            plotter=None,

            policy_lr=3e-4,
            Q_lr=3e-4,
            alpha_lr=3e-4,
            reward_scale=1.0,
            target_entropy='auto',
            discount=0.99,
            tau=5e-3,
            target_update_interval=1,

            save_full_state=False,
            Q_targets=None,
            **kwargs,
    ):
        """
        Args:
            env (`SoftlearningEnv`): Environment used for training.
            policy: A policy function approximator.
            Qs: Q-function approximators. The min of these
                approximators will be used. Usage of at least two Q-functions
                improves performance by reducing overestimation bias.
            plotter (`QFPolicyPlotter`): Plotter instance to be used for
                visualizing Q-function during training.
            lr (`float`): Learning rate used for the function approximators.
            discount (`float`): Discount factor for Q-function updates.
            tau (`float`): Soft value function target update weight.
            target_update_interval ('int'): Frequency at which target network
                updates occur in iterations.
        """

        super(SAC_G, self).__init__(**kwargs)

        self._training_environment = training_environment
        self._evaluation_environment = evaluation_environment
        self._policy = policy

        self._Qs = Qs

        if Q_targets is not None:
            self._Q_targets = Q_targets
        else:
            self._Q_targets = tuple(deepcopy(Q) for Q in Qs)
            self._update_target(tau=tf.constant(1.0))

        self._plotter = plotter

        self._policy_lr = policy_lr
        self._Q_lr = Q_lr
        self._alpha_lr = alpha_lr

        self._reward_scale = reward_scale
        self._target_entropy = (
            heuristic_target_entropy(self._training_environment.action_space)
            if target_entropy == 'auto'
            else target_entropy)

        self._discount = discount
        self._tau = tau
        self._target_update_interval = target_update_interval

        self._save_full_state = save_full_state

        self._Q_optimizers = tuple(
            tf.optimizers.Adam(
                learning_rate=self._Q_lr,
                name=f'Q_{i}_optimizer'
            ) for i, Q in enumerate(self._Qs))

        self._policy_optimizer = tf.optimizers.Adam(
            learning_rate=self._policy_lr,
            name="policy_optimizer")

        self._log_alpha = tf.Variable(0.0)
        self._alpha = tfp.util.DeferredTensor(self._log_alpha, tf.exp)

        self._alpha_optimizer = tf.optimizers.Adam(
            self._alpha_lr, name='alpha_optimizer')
        
        ##################################################
        # Gamma model, adaped from gamma-models repo
        # Start initialization here
        ##################################################
        
        self._gamma_args = Args()

        mkdir(self._gamma_args.save_path)
        set_device(self._gamma_args.device)
        
        observation_dim = tree.flatten(self._training_environment.observation_shape)[0][0]
        action_dim = self._training_environment.action_shape[0]
        
        ## initialize conditional spline flow
        self._gamma_model = make_conditional_flow(observation_dim, self._gamma_args.hidden_dims, {'s': observation_dim, 'a': action_dim})
        
        ## target model is analogous to a target Q-function
        self._target_gamma_model = copy.deepcopy(self._gamma_model)

        ## bootstrapped target distribution is mixture of
        ## single-step gaussian (with weight `1 - discount`)
        ## and target model (with weight `discount`)
        self._gamma_bootstrap = BootstrapTarget(self._target_gamma_model, self._gamma_args.discount)

    @tf.function(experimental_relax_shapes=True)
    def _compute_Q_targets(self, batch):
        next_observations = batch['next_observations']
        rewards = batch['rewards']
        terminals = batch['terminals']

        entropy_scale = tf.convert_to_tensor(self._alpha)
        reward_scale = tf.convert_to_tensor(self._reward_scale)
        discount = tf.convert_to_tensor(self._discount)

        next_actions, next_log_pis = self._policy.actions_and_log_probs(
            next_observations)
        next_Qs_values = tuple(
            Q.values(next_observations, next_actions) for Q in self._Q_targets)
        next_Q_values = tf.reduce_min(next_Qs_values, axis=0)

        Q_targets = compute_Q_targets(
            next_Q_values,
            next_log_pis,
            rewards,
            terminals,
            discount,
            entropy_scale,
            reward_scale)

        return tf.stop_gradient(Q_targets)
    
    def _update_gamma(self, batch):
        
        for i in range(self._gamma_args.n_steps):
            if i < self._gamma_args.n_burnin and False:
                ## initialize model with a lower discount to speed up training
                self._gamma_bootstrap.update_discount(self._gamma_args.burnin_discount)
                sample_discount = self._gamma_args.burnin_discount
            else:
                self._gamma_bootstrap.update_discount(self._gamma_args.discount)
                sample_discount = self._gamma_args.sample_discount
    
        ## batch contains the usual Q-learning entries (s, a, s', r, t)
        # Convert from softlearning batch format to gamma-models format
        batch_torch = dict()
        for key, value in batch.items():
            # print(key)
            flattened = tree.flatten(value)[0]
            if flattened.dtype == np.dtype(np.uint64):
                flattened = flattened.astype(np.dtype(np.int64))
            batch_torch[key] = torch.from_numpy(flattened)
            # print(np.shape(batch_torch[key]))
        # print(batch_torch.keys())
        
        ## condition dicts contain keys (s, a)
        condition_dict, next_condition_dict = format_batch_sac(batch_torch, self._policy)

    @tf.function(experimental_relax_shapes=True)
    def _update_critic(self, batch):
        """Update the Q-function.

        Creates a `tf.optimizer.minimize` operation for updating
        critic Q-function with gradient descent, and appends it to
        `self._training_ops` attribute.

        See Equations (5, 6) in [1], for further information of the
        Q-function update rule.
        """
        Q_targets = self._compute_Q_targets(batch)

        observations = batch['observations']
        actions = batch['actions']
        rewards = batch['rewards']

        tf.debugging.assert_shapes((
            (Q_targets, ('B', 1)), (rewards, ('B', 1))))

        Qs_values = []
        Qs_losses = []
        for Q, optimizer in zip(self._Qs, self._Q_optimizers):
            with tf.GradientTape() as tape:
                Q_values = Q.values(observations, actions)
                Q_losses = 0.5 * (
                    tf.losses.MSE(y_true=Q_targets, y_pred=Q_values))
                Q_loss = tf.nn.compute_average_loss(Q_losses)

            gradients = tape.gradient(Q_loss, Q.trainable_variables)
            optimizer.apply_gradients(zip(gradients, Q.trainable_variables))
            Qs_losses.append(Q_losses)
            Qs_values.append(Q_values)

        return Qs_values, Qs_losses

    @tf.function(experimental_relax_shapes=True)
    def _update_actor(self, batch):
        """Update the policy.

        Creates a `tf.optimizer.minimize` operations for updating
        policy and entropy with gradient descent, and adds them to
        `self._training_ops` attribute.

        See Section 4.2 in [1], for further information of the policy update,
        and Section 5 in [1] for further information of the entropy update.
        """
        observations = batch['observations']
        entropy_scale = tf.convert_to_tensor(self._alpha)

        with tf.GradientTape() as tape:
            actions, log_pis = self._policy.actions_and_log_probs(observations)

            Qs_log_targets = tuple(
                Q.values(observations, actions) for Q in self._Qs)
            Q_log_targets = tf.reduce_mean(Qs_log_targets, axis=0)
            policy_losses = entropy_scale * log_pis - Q_log_targets
            policy_loss = tf.nn.compute_average_loss(policy_losses)

        tf.debugging.assert_shapes((
            (actions, ('B', 'nA')),
            (log_pis, ('B', 1)),
            (policy_losses, ('B', 1)),
        ))

        policy_gradients = tape.gradient(
            policy_loss, self._policy.trainable_variables)

        self._policy_optimizer.apply_gradients(zip(
            policy_gradients, self._policy.trainable_variables))

        return policy_losses

    @tf.function(experimental_relax_shapes=True)
    def _update_alpha(self, batch):
        if not isinstance(self._target_entropy, Number):
            return 0.0

        observations = batch['observations']

        actions, log_pis = self._policy.actions_and_log_probs(observations)

        with tf.GradientTape() as tape:
            alpha_losses = -1.0 * (
                self._alpha * tf.stop_gradient(log_pis + self._target_entropy))
            # NOTE(hartikainen): It's important that we take the average here,
            # otherwise we end up effectively having `batch_size` times too
            # large learning rate.
            alpha_loss = tf.nn.compute_average_loss(alpha_losses)

        alpha_gradients = tape.gradient(alpha_loss, [self._log_alpha])
        self._alpha_optimizer.apply_gradients(zip(
            alpha_gradients, [self._log_alpha]))

        return alpha_losses

    @tf.function(experimental_relax_shapes=True)
    def _update_target(self, tau):
        for Q, Q_target in zip(self._Qs, self._Q_targets):
            for source_weight, target_weight in zip(
                    Q.trainable_variables, Q_target.trainable_variables):
                target_weight.assign(
                    tau * source_weight + (1.0 - tau) * target_weight)

    @tf.function(experimental_relax_shapes=True)
    def _do_updates(self, batch):
        """Runs the update operations for policy, Q, and alpha."""
        Qs_values, Qs_losses = self._update_critic(batch)
        policy_losses = self._update_actor(batch)
        alpha_losses = self._update_alpha(batch)
        self._update_gamma(batch)

        diagnostics = OrderedDict((
            ('Q_value-mean', tf.reduce_mean(Qs_values)),
            ('Q_loss-mean', tf.reduce_mean(Qs_losses)),
            ('policy_loss-mean', tf.reduce_mean(policy_losses)),
            ('alpha', tf.convert_to_tensor(self._alpha)),
            ('alpha_loss-mean', tf.reduce_mean(alpha_losses)),
        ))
        return diagnostics

    def _do_training(self, iteration, batch):
        training_diagnostics = self._do_updates(batch)

        if iteration % self._target_update_interval == 0:
            # Run target ops here.
            self._update_target(tau=tf.constant(self._tau))

        return training_diagnostics

    def get_diagnostics(self,
                        iteration,
                        batch,
                        training_paths,
                        evaluation_paths):
        """Return diagnostic information as an ordered dictionary.

        Also calls the `draw` method of the plotter, if plotter defined.
        """
        diagnostics = OrderedDict((
            ('alpha', self._alpha.numpy()),
            ('policy', self._policy.get_diagnostics_np(batch['observations'])),
        ))

        if self._plotter:
            self._plotter.draw()

        return diagnostics

    @property
    def tf_saveables(self):
        saveables = {
            '_policy_optimizer': self._policy_optimizer,
            **{
                f'Q_optimizer_{i}': optimizer
                for i, optimizer in enumerate(self._Q_optimizers)
            },
            '_alpha': self._alpha,
        }

        if hasattr(self, '_alpha_optimizer'):
            saveables['_alpha_optimizer'] = self._alpha_optimizer

        return saveables
