# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Train and Eval DQN on Atari environments.

Training and evaluation proceeds alternately in iterations, where each
iteration consists of a 1M frame training phase followed by a 500K frame
evaluation phase. In the literature, some papers report averages of the train
phases, while others report averages of the eval phases.

This example is configured to use dopamine.atari.preprocessing, which, among
other things, repeats every action it receives for 4 frames, and then returns
the max-pool over the last 2 frames in the group. In this example, when we
refer to "ALE frames" we refer to the frames before the max-pooling step (i.e.
the raw data available for processing). Because of this, many of the
configuration parameters (like initial_collect_steps) are divided by 4 in the
body of the trainer (e.g. if you want to evaluate with 400 frames in the
initial collection, you actually only need to .step the environment 100 times).

For a good survey of training on Atari, see Machado, et al. 2017:
https://arxiv.org/pdf/1709.06009.pdf.

To run:

```bash
tf_agents/agents/dqn/examples/train_eval_atari \
 --root_dir=$HOME/atari/pong \
 --atari_roms_path=/tmp
 --alsologtostderr
```
END GOOGLE-INTERNAL
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from absl import flags

import numpy as np
import tensorflow as tf

from tf_agents.agents.dqn import dqn_agent
from tf_agents.agents.dqn import q_network
from tf_agents.environments import batched_py_environment
from tf_agents.environments import suite_atari
from tf_agents.environments import time_step as ts
from tf_agents.environments import trajectory
from tf_agents.metrics import py_metric
from tf_agents.metrics import py_metrics
from tf_agents.policies import epsilon_greedy_policy
from tf_agents.policies import policy_step
from tf_agents.policies import py_tf_policy
from tf_agents.policies import random_py_policy
from tf_agents.replay_buffers import py_hashed_replay_buffer
from tf_agents.specs import tensor_spec
from tf_agents.utils import common as common_utils
from tf_agents.utils import timer
import gin.tf

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_string('game_name', 'Pong', 'Name of Atari game to run.')
FLAGS = flags.FLAGS

# AtariPreprocessing runs 4 frames at a time, max-pooling over the last 2
# frames. We need to account for this when computing things like update
# intervals.
ATARI_FRAME_SKIP = 4


class AtariQNetwork(q_network.QNetwork):
  """QNetwork subclass that divides observations by 255."""

  def call(self, observation, step_type=None, network_state=None):
    state = tf.to_float(observation)
    # We divide the grayscale pixel values by 255 here rather than storing
    # normalized values beause uint8s are 4x cheaper to store than float32s.
    state = tf.div(state, 255.)
    return super(AtariQNetwork, self).call(
        state, step_type=step_type, network_state=network_state)


def log_metric(metric, prefix):
  tag = common_utils.join_scope(prefix, metric.name)
  tf.logging.info('{0} = {1}'.format(tag, metric.result()))


@gin.configurable
class TrainEval(object):
  """Train and evaluate DQN on Atari."""

  def __init__(
      self,
      root_dir,
      env_name,
      num_iterations=200,
      max_episode_frames=108000,  # ALE frames
      terminal_on_life_loss=False,
      conv_layer_params=(
          (32, (8, 8), 4), (64, (4, 4), 2), (64, (3, 3), 1)),
      fc_layer_params=(512,),
      # Params for collect
      initial_collect_steps=80000,  # ALE frames
      epsilon_greedy=0.01,
      epsilon_decay_period=1000000,  # ALE frames
      replay_buffer_capacity=1000000,
      # Params for train
      train_steps_per_iteration=1000000,  # ALE frames
      update_period=16,  # ALE frames
      target_update_tau=1.0,
      target_update_period=32000,  # ALE frames
      batch_size=32,
      learning_rate=2.5e-4,
      gamma=0.99,
      reward_scale_factor=1.0,
      gradient_clipping=None,
      # Params for eval
      do_eval=True,
      eval_steps_per_iteration=500000,  # ALE frames
      eval_epsilon_greedy=0.001,
      # Params for checkpoints, summaries, and logging
      log_interval=1000,
      summary_interval=1000,
      summaries_flush_secs=10,
      debug_summaries=False,
      summarize_grads_and_vars=False,
      eval_metrics_callback=None):
    """A simple Atari train and eval for DQN.

    Args:
      root_dir: Directory to write log files to.
      env_name: Fully-qualified name of the Atari environment (i.e. Pong-v0).
      num_iterations: Number of train/eval iterations to run.
      max_episode_frames: Maximum length of a single episode, in ALE frames.
      terminal_on_life_loss: Whether to simulate an episode termination when a
        life is lost.
      conv_layer_params: Params for convolutional layers of QNetwork.
      fc_layer_params: Params for fully connected layers of QNetwork.
      initial_collect_steps: Number of frames to ALE frames to process before
        beginning to train. Since this is in ALE frames, there will be
        initial_collect_steps/4 items in the RB when training starts.
      epsilon_greedy: Final epsilon value to decay to for training.
      epsilon_decay_period: Period over which to decay epsilon, from 1.0 to
        epsilon_greedy (defined above).
      replay_buffer_capacity: Maximum number of items to store in the RB.
      train_steps_per_iteration: Number of ALE frames to run through for each
        iteration of training.
      update_period: Run a train operation every update_period ALE frames.
      target_update_tau: Coeffecient for soft target network updates (1.0 ==
        hard updates).
      target_update_period: Period, in ALE frames, to copy the live network to
        the target network.
      batch_size: Number of frames to include in each training batch.
      learning_rate: RMS optimizer learning rate.
      gamma: Discount for future rewards.
      reward_scale_factor: Scaling factor for rewards.
      gradient_clipping: Norm length to clip gradients.
      do_eval: If True, run an eval every iteration. If False, skip eval.
      eval_steps_per_iteration: Number of ALE frames to run through for each
        iteration of training.
      eval_epsilon_greedy: Epsilon value to use for the evaluation policy (0 ==
        totally greedy policy).
      log_interval: Log stats to the terminal every log_interval training
        steps.
      summary_interval: Write TF summaries every summary_interval training
        steps.
      summaries_flush_secs: Flush summaries to disk every summaries_flush_secs
        seconds.
      debug_summaries: If True, write additional summaries for debugging (see
        dqn_agent for which summaries are written).
      summarize_grads_and_vars: Include gradients in summaries.
      eval_metrics_callback: A callback function that takes (metric_dict,
        global_step) as parameters. Called after every eval with the results of
        the evaluation.
    """
    self._update_period = update_period / ATARI_FRAME_SKIP
    self._train_steps_per_iteration = (train_steps_per_iteration
                                       / ATARI_FRAME_SKIP)
    self._do_eval = do_eval
    self._eval_steps_per_iteration = eval_steps_per_iteration / ATARI_FRAME_SKIP
    self._eval_epsilon_greedy = eval_epsilon_greedy
    self._initial_collect_steps = initial_collect_steps / ATARI_FRAME_SKIP
    self._summary_interval = summary_interval
    self._num_iterations = num_iterations
    self._log_interval = log_interval
    self._eval_metrics_callback = eval_metrics_callback

    with gin.unlock_config():
      gin.bind_parameter('AtariPreprocessing.terminal_on_life_loss',
                         terminal_on_life_loss)

    root_dir = os.path.expanduser(root_dir)
    train_dir = os.path.join(root_dir, 'train')
    eval_dir = os.path.join(root_dir, 'eval')

    train_summary_writer = tf.contrib.summary.create_file_writer(
        train_dir, flush_millis=summaries_flush_secs * 1000)
    train_summary_writer.set_as_default()

    if self._do_eval:
      eval_summary_writer = tf.contrib.summary.create_file_writer(
          eval_dir, flush_millis=summaries_flush_secs * 1000)
      self._eval_metrics = [
          py_metrics.AverageReturnMetric(
              name='PhaseAverageReturn', buffer_size=np.inf),
          py_metrics.AverageEpisodeLengthMetric(
              name='PhaseAverageEpisodeLength', buffer_size=np.inf),
      ]

    with tf.contrib.summary.record_summaries_every_n_global_steps(
        self._summary_interval):

      self._env = suite_atari.load(
          env_name,
          max_episode_steps=max_episode_frames / ATARI_FRAME_SKIP,
          gym_env_wrappers=suite_atari.DEFAULT_ATARI_GYM_WRAPPERS_WITH_STACKING)
      self._env = batched_py_environment.BatchedPyEnvironment([self._env])

      observation_spec = tensor_spec.from_spec(self._env.observation_spec())
      time_step_spec = ts.time_step_spec(observation_spec)
      action_spec = tensor_spec.from_spec(self._env.action_spec())

      self._global_step = tf.train.get_or_create_global_step()

      with tf.device('/cpu:0'):
        epsilon = tf.train.polynomial_decay(
            1.0, self._global_step,
            epsilon_decay_period / ATARI_FRAME_SKIP / self._update_period,
            end_learning_rate=epsilon_greedy)

      with tf.device('/gpu:0'):
        optimizer = tf.train.RMSPropOptimizer(
            learning_rate=learning_rate,
            decay=0.95,
            momentum=0.0,
            epsilon=0.00001,
            centered=True)
        q_net = AtariQNetwork(
            observation_spec,
            action_spec,
            conv_layer_params=conv_layer_params,
            fc_layer_params=fc_layer_params)
        tf_agent = dqn_agent.DqnAgent(
            time_step_spec,
            action_spec,
            q_network=q_net,
            optimizer=optimizer,
            epsilon_greedy=epsilon,
            target_update_tau=target_update_tau,
            target_update_period=(
                target_update_period / ATARI_FRAME_SKIP / self._update_period),
            td_errors_loss_fn=dqn_agent.element_wise_huber_loss,
            gamma=gamma,
            reward_scale_factor=reward_scale_factor,
            gradient_clipping=gradient_clipping,
            debug_summaries=debug_summaries,
            summarize_grads_and_vars=summarize_grads_and_vars)

        self._collect_policy = py_tf_policy.PyTFPolicy(
            tf_agent.collect_policy())

        if self._do_eval:
          self._eval_policy = py_tf_policy.PyTFPolicy(
              epsilon_greedy_policy.EpsilonGreedyPolicy(
                  policy=tf_agent.policy(),
                  epsilon=self._eval_epsilon_greedy))

        py_observation_spec = self._env.observation_spec()
        py_time_step_spec = ts.time_step_spec(py_observation_spec)
        py_action_spec = policy_step.PolicyStep(self._env.action_spec())
        data_spec = trajectory.from_transition(
            py_time_step_spec, py_action_spec, py_time_step_spec)
        self._replay_buffer = (
            py_hashed_replay_buffer.PyHashedReplayBuffer(
                data_spec=data_spec, capacity=replay_buffer_capacity))
        ds = self._replay_buffer.as_dataset(
            sample_batch_size=batch_size, num_steps=2).prefetch(4)
        self._ds_itr = ds.make_initializable_iterator()
        experience = self._ds_itr.get_next()

        self._train_op = tf_agent.train(
            experience,
            train_step_counter=self._global_step)

        self._summary_op = tf.contrib.summary.all_summary_ops()

        self._env_steps_metric = py_metrics.EnvironmentSteps()
        self._step_metrics = [
            py_metrics.NumberOfEpisodes(),
            self._env_steps_metric,
        ]
        self._train_metrics = self._step_metrics + [
            py_metrics.AverageReturnMetric(buffer_size=10),
            py_metrics.AverageEpisodeLengthMetric(buffer_size=10),
        ]
        # The _train_phase_metrics average over an entire train iteration,
        # rather than the rolling average of the last 10 episodes.
        self._train_phase_metrics = [
            py_metrics.AverageReturnMetric(
                name='PhaseAverageReturn', buffer_size=np.inf),
            py_metrics.AverageEpisodeLengthMetric(
                name='PhaseAverageEpisodeLength', buffer_size=np.inf),
        ]
        self._iteration_metric = py_metrics.CounterMetric(name='Iteration')

        # Summaries written from python should run every time they are
        # generated.
        with tf.contrib.summary.always_record_summaries():
          self._steps_per_second_ph = tf.placeholder(
              tf.float32, shape=(), name='steps_per_sec_ph')
          self._steps_per_second_summary = tf.contrib.summary.scalar(
              name='global_steps/sec', tensor=self._steps_per_second_ph)

          for metric in self._train_metrics:
            metric.tf_summaries(step_metrics=self._step_metrics)

          for metric in self._train_phase_metrics:
            metric.tf_summaries(step_metrics=(self._iteration_metric,))
          self._iteration_metric.tf_summaries()

          if self._do_eval:
            with eval_summary_writer.as_default():
              for metric in self._eval_metrics:
                metric.tf_summaries(step_metrics=(self._iteration_metric,))

        self._train_checkpointer = common_utils.Checkpointer(
            ckpt_dir=train_dir,
            agent=tf_agent,
            global_step=self._global_step,
            optimizer=optimizer,
            metrics=tf.contrib.checkpoint.List(
                self._train_metrics + self._train_phase_metrics +
                [self._iteration_metric]))
        self._policy_checkpointer = common_utils.Checkpointer(
            ckpt_dir=os.path.join(train_dir, 'policy'),
            policy=tf_agent.policy(),
            global_step=self._global_step)
        self._rb_checkpointer = common_utils.Checkpointer(
            ckpt_dir=os.path.join(train_dir, 'replay_buffer'),
            max_to_keep=1,
            replay_buffer=self._replay_buffer)

        self._init_agent_op = tf_agent.initialize()

  def game_over(self):
    return self._env.envs[0].game_over

  def run(self):
    """Execute the train/eval loop."""
    with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:
      # Initialize the graph.
      self._initialize_graph(sess)
      tf.get_default_graph().finalize()

      # Initial collect
      self._initial_collect()

      while self._iteration_metric.result() < self._num_iterations:
        # Train phase
        env_steps = 0
        for metric in self._train_phase_metrics:
          metric.reset()
        while env_steps < self._train_steps_per_iteration:
          env_steps += self._run_episode(
              sess, self._train_metrics + self._train_phase_metrics, train=True)
        for metric in self._train_phase_metrics:
          log_metric(metric, prefix='Train/Metrics')
        py_metric.run_summaries(
            self._train_phase_metrics + [self._iteration_metric])

        global_step_val = sess.run(self._global_step)

        if self._do_eval:
          # Eval phase
          env_steps = 0
          for metric in self._eval_metrics:
            metric.reset()
          while env_steps < self._eval_steps_per_iteration:
            env_steps += self._run_episode(
                sess, self._eval_metrics, train=False)

          py_metric.run_summaries(self._eval_metrics + [self._iteration_metric])
          if self._eval_metrics_callback:
            results = dict((metric.name, metric.result())
                           for metric in self._eval_metrics)
            self._eval_metrics_callback(results, global_step_val)
          for metric in self._eval_metrics:
            log_metric(metric, prefix='Eval/Metrics')

        self._iteration_metric()

        self._train_checkpointer.save(global_step=global_step_val)
        self._policy_checkpointer.save(global_step=global_step_val)
        self._rb_checkpointer.save(global_step=global_step_val)

  def _initialize_graph(self, sess):
    """Initialize the graph for sess."""
    self._train_checkpointer.initialize_or_restore(sess)
    self._rb_checkpointer.initialize_or_restore(sess)
    # TODO(sguada) Remove once Periodically can be saved.
    common_utils.initialize_uninitialized_variables(sess)

    sess.run(self._ds_itr.initializer)
    sess.run(self._init_agent_op)

    self._train_step_call = sess.make_callable(
        [self._train_op, self._summary_op])

    self._collect_timer = timer.Timer()
    self._train_timer = timer.Timer()
    self._action_timer = timer.Timer()
    self._step_timer = timer.Timer()
    self._observer_timer = timer.Timer()

    global_step_val = sess.run(self._global_step)
    self._timed_at_step = global_step_val

    # Call save to initialize the save_counter (need to do this before
    # finalizing the graph).
    self._train_checkpointer.save(global_step=global_step_val)
    self._policy_checkpointer.save(global_step=global_step_val)
    self._rb_checkpointer.save(global_step=global_step_val)

    tf.contrib.summary.initialize(session=sess, graph=tf.get_default_graph())

  def _initial_collect(self):
    """Collect initial experience before training begins."""
    tf.logging.info('Collecting initial experience...')
    time_step_spec = ts.time_step_spec(self._env.observation_spec())
    random_policy = random_py_policy.RandomPyPolicy(
        time_step_spec, self._env.action_spec())
    time_step = self._env.reset()
    while self._replay_buffer.size < self._initial_collect_steps:
      if self.game_over():
        time_step = self._env.reset()
      action_step = random_policy.action(time_step)
      next_time_step = self._env.step(action_step.action)
      self._replay_buffer.add_batch(trajectory.from_transition(
          time_step, action_step, next_time_step))
      time_step = next_time_step
    tf.logging.info('Done.')

  def _run_episode(self, sess, metric_observers, train=False):
    """Run a single episode."""
    env_steps = 0
    time_step = self._env.reset()
    while True:
      with self._collect_timer:
        time_step = self._collect_step(
            time_step,
            self._collect_policy,
            metric_observers,
            train=train)
        env_steps += 1

      if self.game_over():
        break
      elif train and self._env_steps_metric.result() % self._update_period == 0:
        with self._train_timer:
          total_loss, _ = self._train_step_call()
          global_step_val = sess.run(self._global_step)
        self._maybe_log(sess, global_step_val, total_loss)
        self._maybe_record_summaries(global_step_val)

    return env_steps

  def _observe(self, metric_observers, traj):
    with self._observer_timer:
      for observer in metric_observers:
        observer(traj)

  def _store_to_rb(self, traj):
    # Clip the reward to (-1, 1) to normalize rewards in training.
    traj = traj._replace(
        reward=np.asarray(np.clip(traj.reward, -1, 1)))
    self._replay_buffer.add_batch(traj)

  def _collect_step(self, time_step, policy, metric_observers, train=False):
    """Run a single step (or 2 steps on life loss) in the environment."""
    with self._action_timer:
      action_step = policy.action(time_step)
    with self._step_timer:
      next_time_step = self._env.step(action_step.action)
      traj = trajectory.from_transition(time_step, action_step, next_time_step)

    if next_time_step.is_last() and not self.game_over():
      traj = traj._replace(discount=np.array([1.0], dtype=np.float32))

    if train:
      self._store_to_rb(traj)

    # When AtariPreprocessing.terminal_on_life_loss is True, we receive LAST
    # time_steps when lives are lost but the game is not over.In this mode, the
    # replay buffer and agent's policy must see the life loss as a LAST step
    # and the subsequent step as a FIRST step. However, we do not want to
    # actually terminate the episode and metrics should be computed as if all
    # steps were MID steps, since life loss is not actually a terminal event
    # (it is mostly a trick to make it easier to propagate rewards backwards by
    # shortening episode durations from the agent's perspective).
    if next_time_step.is_last() and not self.game_over():
      # Update metrics as if this is a mid-episode step.
      next_time_step = ts.transition(
          next_time_step.observation, next_time_step.reward)
      self._observe(metric_observers, trajectory.from_transition(
          time_step, action_step, next_time_step))

      # Produce the next step as if this is the first step of an episode and
      # store to RB as such. The next_time_step will be a MID time step.
      reward = time_step.reward
      time_step = ts.restart(next_time_step.observation)
      with self._action_timer:
        action_step = policy.action(time_step)
      with self._step_timer:
        next_time_step = self._env.step(action_step.action)
      if train:
        self._store_to_rb(trajectory.from_transition(
            time_step, action_step, next_time_step))

      # Update metrics as if this is a mid-episode step.
      time_step = ts.transition(time_step.observation, reward)
      traj = trajectory.from_transition(time_step, action_step, next_time_step)

    self._observe(metric_observers, traj)

    return next_time_step

  def _maybe_record_summaries(self, global_step_val):
    """Record summaries if global_step_val is a multiple of summary_interval."""
    if global_step_val % self._summary_interval == 0:
      py_metric.run_summaries(self._train_metrics)

  def _maybe_log(self, sess, global_step_val, total_loss):
    """Log some stats if global_step_val is a multiple of log_interval."""
    if global_step_val % self._log_interval == 0:
      tf.logging.info('step = %d, loss = %f', global_step_val, total_loss.loss)
      tf.logging.info('action_time = {}'.format(self._action_timer.value()))
      tf.logging.info('step_time = {}'.format(self._step_timer.value()))
      tf.logging.info('oberver_time = {}'.format(self._observer_timer.value()))
      steps_per_sec = ((global_step_val - self._timed_at_step) /
                       (self._collect_timer.value()
                        + self._train_timer.value()))
      sess.run(self._steps_per_second_summary,
               feed_dict={self._steps_per_second_ph: steps_per_sec})
      tf.logging.info('%.3f steps/sec' % steps_per_sec)
      tf.logging.info('collect_time = {}, train_time = {}'.format(
          self._collect_timer.value(), self._train_timer.value()))
      for metric in self._train_metrics:
        log_metric(metric, prefix='Train/Metrics')
      self._timed_at_step = global_step_val
      self._collect_timer.reset()
      self._train_timer.reset()
      self._action_timer.reset()
      self._step_timer.reset()
      self._observer_timer.reset()


def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)
  TrainEval(FLAGS.root_dir, suite_atari.game(name=FLAGS.game_name)).run()


if __name__ == '__main__':
  flags.mark_flag_as_required('root_dir')
  tf.app.run()
