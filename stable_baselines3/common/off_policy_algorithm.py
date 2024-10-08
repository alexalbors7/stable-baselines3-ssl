import io
import pathlib
import sys
import time
import warnings
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

import numpy as np
import torch as th
from gymnasium import spaces
import time
from sklearn.metrics import f1_score
import graphlearning as gl

from stable_baselines3.common.base_class import BaseAlgorithm
from stable_baselines3.common.buffers import DictReplayBuffer, ReplayBuffer
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.noise import ActionNoise, VectorizedActionNoise
from stable_baselines3.common.policies import BasePolicy
from stable_baselines3.common.save_util import load_from_pkl, save_to_pkl
from stable_baselines3.common.type_aliases import GymEnv, MaybeCallback, RolloutReturn, Schedule, TrainFreq, TrainFrequencyUnit
from stable_baselines3.common.utils import safe_mean, should_collect_more_steps, construct_connected_W, infer_rewards_SSL, rewards_to_labels
from stable_baselines3.common.vec_env import VecEnv
from stable_baselines3.her.her_replay_buffer import HerReplayBuffer

SelfOffPolicyAlgorithm = TypeVar("SelfOffPolicyAlgorithm", bound="OffPolicyAlgorithm")


class OffPolicyAlgorithm(BaseAlgorithm):
    """
    The base for Off-Policy algorithms (ex: SAC/TD3)

    :param policy: The policy model to use (MlpPolicy, CnnPolicy, ...)
    :param env: The environment to learn from
                (if registered in Gym, can be str. Can be None for loading trained models)
    :param learning_rate: learning rate for the optimizer,
        it can be a function of the current progress remaining (from 1 to 0)
    :param buffer_size: size of the replay buffer
    :param learning_starts: how many steps of the model to collect transitions for before learning starts
    :param batch_size: Minibatch size for each gradient update
    :param tau: the soft update coefficient ("Polyak update", between 0 and 1)
    :param gamma: the discount factor
    :param train_freq: Update the model every ``train_freq`` steps. Alternatively pass a tuple of frequency and unit
        like ``(5, "step")`` or ``(2, "episode")``.
    :param gradient_steps: How many gradient steps to do after each rollout (see ``train_freq``)
        Set to ``-1`` means to do as many gradient steps as steps done in the environment
        during the rollout.
    :param action_noise: the action noise type (None by default), this can help
        for hard exploration problem. Cf common.noise for the different action noise type.
    :param replay_buffer_class: Replay buffer class to use (for instance ``HerReplayBuffer``).
        If ``None``, it will be automatically selected.
    :param replay_buffer_kwargs: Keyword arguments to pass to the replay buffer on creation.
    :param optimize_memory_usage: Enable a memory efficient variant of the replay buffer
        at a cost of more complexity.
        See https://github.com/DLR-RM/stable-baselines3/issues/37#issuecomment-637501195
    :param policy_kwargs: Additional arguments to be passed to the policy on creation
    :param stats_window_size: Window size for the rollout logging, specifying the number of episodes to average
        the reported success rate, mean episode length, and mean reward over
    :param tensorboard_log: the log location for tensorboard (if None, no logging)
    :param verbose: Verbosity level: 0 for no output, 1 for info messages (such as device or wrappers used), 2 for
        debug messages
    :param device: Device on which the code should run.
        By default, it will try to use a Cuda compatible device and fallback to cpu
        if it is not possible.
    :param support_multi_env: Whether the algorithm supports training
        with multiple environments (as in A2C)
    :param monitor_wrapper: When creating an environment, whether to wrap it
        or not in a Monitor wrapper.
    :param seed: Seed for the pseudo random generators
    :param use_sde: Whether to use State Dependent Exploration (SDE)
        instead of action noise exploration (default: False)
    :param sde_sample_freq: Sample a new noise matrix every n steps when using gSDE
        Default: -1 (only sample at the beginning of the rollout)
    :param use_sde_at_warmup: Whether to use gSDE instead of uniform sampling
        during the warm up phase (before learning starts)
    :param sde_support: Whether the model support gSDE or not
    :param supported_action_spaces: The action spaces supported by the algorithm.
    """

    actor: th.nn.Module

    def __init__(
        self,
        policy: Union[str, Type[BasePolicy]],
        env: Union[GymEnv, str],
        learning_rate: Union[float, Schedule],
        buffer_size: int = 1_000_000,  # 1e6
        unlabeled_buffer_size: int = 100_000,
        ssl_buffer_size: int = 100_000,
        learning_starts: int = 100,
        batch_size: int = 256,
        ssl_batch_size: int = 512,
        tau: float = 0.005,
        gamma: float = 0.99,
        train_freq: Union[int, Tuple[int, str]] = (1, "step"),
        gradient_steps: int = 1,
        action_noise: Optional[ActionNoise] = None,
        replay_buffer_class: Optional[Type[ReplayBuffer]] = None,
        replay_buffer_kwargs: Optional[Dict[str, Any]] = None,
        optimize_memory_usage: bool = False,
        policy_kwargs: Optional[Dict[str, Any]] = None,
        stats_window_size: int = 100,
        tensorboard_log: Optional[str] = None,
        verbose: int = 0,
        device: Union[th.device, str] = "auto",
        support_multi_env: bool = False,
        monitor_wrapper: bool = True,
        seed: Optional[int] = None,
        use_sde: bool = False,
        sde_sample_freq: int = -1,
        use_sde_at_warmup: bool = False,
        sde_support: bool = True,
        supported_action_spaces: Optional[Tuple[Type[spaces.Space], ...]] = None,
        p: float = 1.,
        pseudo_mode: bool = False,
        ssl_freq: int = 100,
        method: str = 'Laplace'        
    ):
        super().__init__(
            policy=policy,
            env=env,
            learning_rate=learning_rate,
            policy_kwargs=policy_kwargs,
            stats_window_size=stats_window_size,
            tensorboard_log=tensorboard_log,
            verbose=verbose,
            device=device,
            support_multi_env=support_multi_env,
            monitor_wrapper=monitor_wrapper,
            seed=seed,
            use_sde=use_sde,
            sde_sample_freq=sde_sample_freq,
            supported_action_spaces=supported_action_spaces,
        )
        
        self.method = method
        self.buffer_size = buffer_size 
        self.unlabeled_buffer_size = unlabeled_buffer_size
        self.ssl_buffer_size = ssl_buffer_size
        self.batch_size = batch_size
        self.ssl_batch_size = ssl_batch_size
        self.learning_starts = learning_starts
        self.tau = tau
        self.gamma = gamma
        self.gradient_steps = gradient_steps
        self.action_noise = action_noise
        self.optimize_memory_usage = optimize_memory_usage
        self.replay_buffer: Optional[ReplayBuffer] = None
        self.replay_buffer_class = replay_buffer_class
        self.replay_buffer_kwargs = replay_buffer_kwargs or {}
        self.unlabeled_replay_buffer: Optional[ReplayBuffer] = None
        self._episode_storage = None

        # Probability transition is labeled or not. 
        self.p = p
        self.pseudo_mode = pseudo_mode

        # Save train and ssl freq parameter, will be converted later to TrainFreq object
        self.train_freq = train_freq

        # SSL info
        self.ssl_freq = ssl_freq

        # Update policy keyword arguments
        if sde_support:
            self.policy_kwargs["use_sde"] = self.use_sde
        # For gSDE only
        self.use_sde_at_warmup = use_sde_at_warmup

    def _convert_train_freq(self) -> None:
        """
        Convert `train_freq` parameter (int or tuple)
        to a TrainFreq object.
        """
        if not isinstance(self.train_freq, TrainFreq):
            train_freq = self.train_freq

            # The value of the train frequency will be checked later
            if not isinstance(train_freq, tuple):
                train_freq = (train_freq, "step")

            try:
                train_freq = (train_freq[0], TrainFrequencyUnit(train_freq[1]))  # type: ignore[assignment]
            except ValueError as e:
                raise ValueError(
                    f"The unit of the `train_freq` must be either 'step' or 'episode' not '{train_freq[1]}'!"
                ) from e

            if not isinstance(train_freq[0], int):
                raise ValueError(f"The frequency of `train_freq` must be an integer and not {train_freq[0]}")

            self.train_freq = TrainFreq(*train_freq)  # type: ignore[assignment,arg-type]

    def _setup_model(self) -> None:
        self._setup_lr_schedule()
        self.set_random_seed(self.seed)

        if self.replay_buffer_class is None:
            if isinstance(self.observation_space, spaces.Dict):
                self.replay_buffer_class = DictReplayBuffer
            else:
                self.replay_buffer_class = ReplayBuffer

        if self.replay_buffer is None:
            # Make a local copy as we should not pickle
            # the environment when using HerReplayBuffer
            replay_buffer_kwargs = self.replay_buffer_kwargs.copy()
            if issubclass(self.replay_buffer_class, HerReplayBuffer):
                assert self.env is not None, "You must pass an environment when using `HerReplayBuffer`"
                replay_buffer_kwargs["env"] = self.env

            self.replay_buffer = self.replay_buffer_class(
                self.buffer_size,
                self.observation_space,
                self.action_space,
                device=self.device,
                n_envs=self.n_envs,
                optimize_memory_usage=self.optimize_memory_usage,
                **replay_buffer_kwargs,
                pseudo_mode=False
            )

            self.unlabeled_replay_buffer = None

            if self.pseudo_mode:
                self.ssl_replay_buffer = self.replay_buffer_class(
                    self.ssl_buffer_size,
                    self.observation_space,
                    self.action_space,
                    device=self.device,
                    n_envs=self.n_envs,
                    optimize_memory_usage=self.optimize_memory_usage,
                    **replay_buffer_kwargs,
                    pseudo_mode=True
                )

                self.unlabeled_replay_buffer = self.replay_buffer_class(
                    self.unlabeled_buffer_size,
                    self.observation_space,
                    self.action_space,
                    device=self.device,
                    n_envs=self.n_envs,
                    optimize_memory_usage=self.optimize_memory_usage,
                    **replay_buffer_kwargs,
                    pseudo_mode=False
                )

        self.policy = self.policy_class(
            self.observation_space,
            self.action_space,
            self.lr_schedule,
            **self.policy_kwargs,
        )
        self.policy = self.policy.to(self.device)

        # Convert train freq parameter to TrainFreq object
        self._convert_train_freq()

    def save_replay_buffer(self, path: Union[str, pathlib.Path, io.BufferedIOBase]) -> None:
        """
        Save the replay buffer as a pickle file.

        :param path: Path to the file where the replay buffer should be saved.
            if path is a str or pathlib.Path, the path is automatically created if necessary.
        """
        assert self.replay_buffer is not None, "The replay buffer is not defined"
        save_to_pkl(path, self.replay_buffer, self.verbose)

    def load_replay_buffer(
        self,
        path: Union[str, pathlib.Path, io.BufferedIOBase],
        truncate_last_traj: bool = True,
    ) -> None:
        """
        Load a replay buffer from a pickle file.

        :param path: Path to the pickled replay buffer.
        :param truncate_last_traj: When using ``HerReplayBuffer`` with online sampling:
            If set to ``True``, we assume that the last trajectory in the replay buffer was finished
            (and truncate it).
            If set to ``False``, we assume that we continue the same trajectory (same episode).
        """
        self.replay_buffer = load_from_pkl(path, self.verbose)
        assert isinstance(self.replay_buffer, ReplayBuffer), "The replay buffer must inherit from ReplayBuffer class"

        # Backward compatibility with SB3 < 2.1.0 replay buffer
        # Keep old behavior: do not handle timeout termination separately
        if not hasattr(self.replay_buffer, "handle_timeout_termination"):  # pragma: no cover
            self.replay_buffer.handle_timeout_termination = False
            self.replay_buffer.timeouts = np.zeros_like(self.replay_buffer.dones)

        if isinstance(self.replay_buffer, HerReplayBuffer):
            assert self.env is not None, "You must pass an environment at load time when using `HerReplayBuffer`"
            self.replay_buffer.set_env(self.env)
            if truncate_last_traj:
                self.replay_buffer.truncate_last_trajectory()

        # Update saved replay buffer device to match current setting, see GH#1561
        self.replay_buffer.device = self.device

    def _setup_learn(
        self,
        total_timesteps: int,
        callback: MaybeCallback = None,
        reset_num_timesteps: bool = True,
        tb_log_name: str = "run",
        progress_bar: bool = False,
    ) -> Tuple[int, BaseCallback]:
        """
        cf `BaseAlgorithm`.
        """
        # Prevent continuity issue by truncating trajectory
        # when using memory efficient replay buffer
        # see https://github.com/DLR-RM/stable-baselines3/issues/46

        replay_buffer = self.replay_buffer

        truncate_last_traj = (
            self.optimize_memory_usage
            and reset_num_timesteps
            and replay_buffer is not None
            and (replay_buffer.full or replay_buffer.pos > 0)
        )

        if truncate_last_traj:
            warnings.warn(
                "The last trajectory in the replay buffer will be truncated, "
                "see https://github.com/DLR-RM/stable-baselines3/issues/46."
                "You should use `reset_num_timesteps=False` or `optimize_memory_usage=False`"
                "to avoid that issue."
            )
            assert replay_buffer is not None  # for mypy
            # Go to the previous index
            pos = (replay_buffer.pos - 1) % replay_buffer.buffer_size
            replay_buffer.dones[pos] = True

        assert self.env is not None, "You must set the environment before calling _setup_learn()"
        # Vectorize action noise if needed
        if (
            self.action_noise is not None
            and self.env.num_envs > 1
            and not isinstance(self.action_noise, VectorizedActionNoise)
        ):
            self.action_noise = VectorizedActionNoise(self.action_noise, self.env.num_envs)

        return super()._setup_learn(
            total_timesteps,
            callback,
            reset_num_timesteps,
            tb_log_name,
            progress_bar,
        )

    def learn(
        self: SelfOffPolicyAlgorithm,
        total_timesteps: int,
        callback: MaybeCallback = None,
        log_interval: int = 4,
        tb_log_name: str = "run",
        reset_num_timesteps: bool = True,
        progress_bar: bool = False,
        verbose: bool = False
    ) -> SelfOffPolicyAlgorithm:
        total_timesteps, callback = self._setup_learn(
            total_timesteps,
            callback,
            reset_num_timesteps,
            tb_log_name,
            progress_bar,
        )

        callback.on_training_start(locals(), globals())
        
        assert self.env is not None, "You must set the environment before calling learn()"
        assert isinstance(self.train_freq, TrainFreq)  # check done in _setup_learn()
        
        while self.num_timesteps < total_timesteps:
            # Model 1:
            # With a certain probability p, obtain the reward. Then we perform SSL on pseduo replay buffer, maybe setting as 'True' if pseudo_reward has ever been computed, that way we avoid using nans for pseudo_reward. Every k steps, perform the SSL update on everything (?) and keep going.
            # 
            # To do so, 2 pseudo_replay_buffers. One with labeled data that gets deleted every ssl_freq iterations, other with entire unlabeled data.  
            # Model 2:
            # Never obtain reward unless model asks for it. Perform active learning by querying r rewards every k steps based on Laplacian uncertainties. 
            rollout = self.collect_rollouts(
                self.env,
                train_freq=self.train_freq,
                action_noise=self.action_noise,
                callback=callback,
                learning_starts=self.learning_starts,
                replay_buffer=self.replay_buffer,
                log_interval=log_interval,
                unlabeled_replay_buffer=self.unlabeled_replay_buffer
            )

            if not rollout.continue_training:
                break
            
            if self.pseudo_mode and self.num_timesteps % self.ssl_freq == 0 and self.num_timesteps > self.learning_starts // 2:
                # Completely reset the ssl_replay_buffer
                # print(self.num_timesteps)
                self.ssl_replay_buffer.reset()
                
                # This is indices for amount of unlabeled data we get
                unlabeled_data_ind = np.arange(self.unlabeled_replay_buffer.size())
                labeled_data_ind = np.arange(self.replay_buffer.size())

                # Get all labeled + unlabeled data
                labeled_replay_data = self.replay_buffer._get_samples(labeled_data_ind)
                unlabeled_replay_data = self.unlabeled_replay_buffer._get_samples(unlabeled_data_ind)

                if verbose:
                    print("Labeled data obs shape", labeled_replay_data.observations.shape)
                    print("Unlabeled data obs shape", unlabeled_replay_data.observations.shape)
                    print("Unlabeled data action shape", unlabeled_replay_data.actions.shape)

                # Pair into (s, a) pairs. (For Active grids it would be easier to use next_obs as the reward setter but kind of cheating)
                # One hot encode actions beforehand

                
                unlabeled_actions_1hot = gl.utils.labels_to_onehot(unlabeled_replay_data.actions.numpy().flatten(), 4).reshape(-1, 4)
                labeled_actions_1hot = gl.utils.labels_to_onehot(labeled_replay_data.actions.numpy().flatten(), 4).reshape(-1, 4)

                unlabeled_state_action = np.concatenate((unlabeled_replay_data.observations, unlabeled_actions_1hot), axis=1)
                labeled_state_action = np.concatenate((labeled_replay_data.observations, labeled_actions_1hot), axis=1)

                labeled_rewards = labeled_replay_data.rewards.numpy()
                unlabeled_rewards = unlabeled_replay_data.rewards.numpy()

                # FIRST ENSURE OBSERVATIONS ARE ALL UNIQUE, BOTH FOR LABELED AND UNLABELED, WHILE RESPECTING RELATIVE ORDER, AND SAVE RELEVANT INDICES
                # Now do this for state, action pairs.  
                # unique_labeled_next_obs, unique_labeled_ind = np.unique(labeled_replay_data.next_observations[:, :2], axis=0, return_index=True)
                # unique_labeled_ind = np.sort(unique_labeled_ind)
                # unique_labeled_next_obs = labeled_replay_data.next_observations[unique_labeled_ind, :2].numpy()
                # unique_labeled_rewards = labeled_rewards[unique_labeled_ind]

                # unique_unlabeled_next_obs, unique_unlabeled_ind = np.unique(unlabeled_replay_data.next_observations[:, :2], axis=0, return_index=True)
                # unique_unlabeled_ind = np.sort(unique_unlabeled_ind)
                # unique_unlabeled_next_obs = unlabeled_replay_data.next_observations[unique_unlabeled_ind, :2].numpy()
                # unique_unlabeled_rewards = unlabeled_rewards[unique_unlabeled_ind]

                unique_unlabeled_sa, unique_unlabeled_ind = np.unique(unlabeled_state_action, axis=0, return_index=True)
                unique_unlabeled_ind = np.sort(unique_unlabeled_ind)
                unique_unlabeled_sa = unlabeled_state_action[unique_unlabeled_ind]
                unique_unlabeled_rewards = unlabeled_rewards[unique_unlabeled_ind]

                unique_labeled_sa, unique_labeled_ind = np.unique(labeled_state_action, axis=0, return_index=True)
                unique_labeled_ind = np.sort(unique_labeled_ind)
                unique_labeled_sa = labeled_state_action[unique_labeled_ind]
                unique_labeled_rewards = labeled_rewards[unique_labeled_ind]

                # THEN CONSTRUCT DATA BEING UNIQUE BY CONCATENATING UNLABELED + LABELED
                #X_prime = np.concatenate((unique_labeled_next_obs, unique_unlabeled_next_obs), axis=0)
                X_prime = np.concatenate((unique_unlabeled_sa, unique_labeled_sa), axis=0)
                # but there might be redundancies between labeled and unlabeled set
                X_prime_unique, concatenated_unique_ind = np.unique(X_prime, axis=0, return_index=True)
                
                # keep them while respecting order by sorting indices beforehand
                concatenated_unique_ind = np.sort(concatenated_unique_ind)
                X_prime_unique = X_prime[concatenated_unique_ind]
                
                W = construct_connected_W(X_prime_unique)

                train_labels, _, _, l2r = rewards_to_labels(rewards = unique_labeled_rewards.astype(int))

                train_ind = np.arange(unique_labeled_sa.shape[0], dtype=int)

                # get actual true labels to log f1_scores
                true_rewards = np.concatenate((unique_labeled_rewards, unique_unlabeled_rewards), axis=0).flatten()[concatenated_unique_ind].astype(int)

                if verbose:
                    print("actions 1 hot", unlabeled_actions_1hot.shape)
                    print("Unlabeled state action 1 hot", unlabeled_state_action.shape)
                    print("Labeled state action 1 hot", labeled_state_action.shape)
                    print("Total number of unlabeled+labeled after uniqueness filtering: ", X_prime_unique.shape[0])
                    print("Train labels", train_labels, type(train_labels), train_labels.shape)
                    print("Train indices", train_ind, type(train_ind), train_ind.shape)
                
                pred_labels, _, _ = infer_rewards_SSL(
                                    method = self.method,
                                    W = W, 
                                    train_labels = train_labels,
                                    train_ind = train_ind,
                                    get_uncertainty = True
                                )
    
                pseudo_rewards = th.tensor(list(map(lambda l: l2r[l.item()], pred_labels)))
                f1 = f1_score(np.delete(pseudo_rewards, train_ind), np.delete(true_rewards,train_ind), average='micro')

                if verbose:
                    print("Pseudo reward shape: ", pseudo_rewards.shape)
                    print("True reward shape: ", true_rewards.shape)
                    print(np.delete(pseudo_rewards, train_ind), np.delete(true_rewards,train_ind))
                    print("Pred labels", pred_labels[:100])
                    print("Train labels", train_labels.shape, train_labels)
                    print(f"F1_score {f1}")
                    print("True rewards",  true_rewards.shape, true_rewards[:100])
                    print("Pseudo rewards", pseudo_rewards.shape, pseudo_rewards[:100].numpy())
                
                self.logger.record("ssl/f1_score", f1)
                # Now we can only extend the unique ones, remember...

                # Basically just unlabeled with different rewards
                self.ssl_replay_buffer.extend (
                    unlabeled_replay_data.observations[unique_unlabeled_ind],  # type: ignore[arg-type]
                    unlabeled_replay_data.next_observations[unique_unlabeled_ind],  #
                    unlabeled_replay_data.actions[unique_unlabeled_ind],
                    unlabeled_replay_data.rewards[unique_unlabeled_ind],
                    unlabeled_replay_data.dones[unique_unlabeled_ind],
                    self.unlabeled_replay_buffer.timeouts[np.arange(self.unlabeled_replay_buffer.size())][unique_unlabeled_ind],
                    np.delete(pseudo_rewards, train_ind) # No need since pseudo_rewards already processed
                )
            
                self.logger.record("buffer/num_ssl_rewards", self.ssl_replay_buffer.size())


            # Train using all labeled and unlabeled data. 
            if self.num_timesteps > 0 and self.num_timesteps > self.learning_starts:
                # If no `gradient_steps` is specified,
                # do as many gradients steps as steps performed during the rollout
                gradient_steps = self.gradient_steps if self.gradient_steps >= 0 else rollout.episode_timesteps
                # Special case when the user passes `gradient_steps=0`
                # Perform gradient descent
                if gradient_steps > 0:
                    self.train(batch_size=self.batch_size, ssl_batch_size=self.ssl_batch_size, gradient_steps=gradient_steps)

        callback.on_training_end()

        return self

    def train(self, gradient_steps: int, batch_size: int, ssl_batch_size: int) -> None:
        """
        Sample the replay buffer and do the updates
        (gradient descent and update target networks)
        """
        raise NotImplementedError()

    def _sample_action(
        self,
        learning_starts: int,
        action_noise: Optional[ActionNoise] = None,
        n_envs: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Sample an action according to the exploration policy.
        This is either done by sampling the probability distribution of the policy,
        or sampling a random action (from a uniform distribution over the action space)
        or by adding noise to the deterministic output.

        :param action_noise: Action noise that will be used for exploration
            Required for deterministic policy (e.g. TD3). This can also be used
            in addition to the stochastic policy for SAC.
        :param learning_starts: Number of steps before learning for the warm-up phase.
        :param n_envs:
        :return: action to take in the environment
            and scaled action that will be stored in the replay buffer.
            The two differs when the action space is not normalized (bounds are not [-1, 1]).
        """
        # Select action randomly or according to policy
        if self.num_timesteps < learning_starts and not (self.use_sde and self.use_sde_at_warmup):
            # Warmup phase
            unscaled_action = np.array([self.action_space.sample() for _ in range(n_envs)])
        else:
            # Note: when using continuous actions,
            # we assume that the policy uses tanh to scale the action
            # We use non-deterministic action in the case of SAC, for TD3, it does not matter
            assert self._last_obs is not None, "self._last_obs was not set"
            " PREDICT ACTION HERE "
            unscaled_action, _ = self.predict(self._last_obs, deterministic=False)

        # Rescale the action from [low, high] to [-1, 1]
        if isinstance(self.action_space, spaces.Box):
            scaled_action = self.policy.scale_action(unscaled_action)

            # Add noise to the action (improve exploration)
            if action_noise is not None:
                scaled_action = np.clip(scaled_action + action_noise(), -1, 1)

            # We store the scaled action in the buffer
            buffer_action = scaled_action
            action = self.policy.unscale_action(scaled_action)
        else:
            # Discrete case, no need to normalize or clip
            buffer_action = unscaled_action
            action = buffer_action
        return action, buffer_action

    def _dump_logs(self) -> None:
        """
        Write log.
        """
        assert self.ep_info_buffer is not None
        assert self.ep_success_buffer is not None

        time_elapsed = max((time.time_ns() - self.start_time) / 1e9, sys.float_info.epsilon)
        fps = int((self.num_timesteps - self._num_timesteps_at_start) / time_elapsed)
        self.logger.record("time/episodes", self._episode_num, exclude="tensorboard")
        if len(self.ep_info_buffer) > 0 and len(self.ep_info_buffer[0]) > 0:
            self.logger.record("rollout/ep_rew_mean", safe_mean([ep_info["r"] for ep_info in self.ep_info_buffer]))
            self.logger.record("rollout/ep_len_mean", safe_mean([ep_info["l"] for ep_info in self.ep_info_buffer]))
        self.logger.record("time/fps", fps)
        self.logger.record("buffer/available_rewards", self.ssl_replay_buffer.size() + self.replay_buffer.size(), exclude="tensorboard")
        self.logger.record("time/time_elapsed", int(time_elapsed), exclude="tensorboard")
        self.logger.record("time/total_timesteps", self.num_timesteps, exclude="tensorboard")
        if self.use_sde:
            self.logger.record("train/std", (self.actor.get_std()).mean().item())

        if len(self.ep_success_buffer) > 0:
            self.logger.record("rollout/success_rate", safe_mean(self.ep_success_buffer))
        # Pass the number of timesteps for tensorboard
        self.logger.dump(step=self.num_timesteps)

    def _on_step(self) -> None:
        """
        Method called after each step in the environment.
        It is meant to trigger DQN target network update
        but can be used for other purposes
        """
        pass

    def _store_transition(
        self,
        true_replay_buffer: ReplayBuffer,
        buffer_action: np.ndarray,
        new_obs: Union[np.ndarray, Dict[str, np.ndarray]],
        reward: np.ndarray,
        dones: np.ndarray,
        infos: List[Dict[str, Any]],
        unlabeled_replay_buffer: ReplayBuffer = None,
    ) -> None:
        """
        Store transition in the replay buffer.
        We store the normalized action and the unnormalized observation.
        It also handles terminal observations (because VecEnv resets automatically).

        :param replay_buffer: Replay buffer object where to store the transition.
        :param buffer_action: normalized action
        :param new_obs: next observation in the current episode
            or first observation of the episode (when dones is True)
        :param reward: reward for the current transition
        :param dones: Termination signal
        :param infos: List of additional information about the transition.
            It may contain the terminal observations and information about timeout.
        """
        # Store only the unnormalized version
        if self._vec_normalize_env is not None:
            new_obs_ = self._vec_normalize_env.get_original_obs()
            reward_ = self._vec_normalize_env.get_original_reward()
        else:
            # Avoid changing the original ones
            self._last_original_obs, new_obs_, reward_ = self._last_obs, new_obs, reward

        # Avoid modification by reference
        next_obs = deepcopy(new_obs_)
        # As the VecEnv resets automatically, new_obs is already the
        # first observation of the next episode
        for i, done in enumerate(dones):
            if done and infos[i].get("terminal_observation") is not None:
                if isinstance(next_obs, dict):
                    next_obs_ = infos[i]["terminal_observation"]
                    # VecNormalize normalizes the terminal observation
                    if self._vec_normalize_env is not None:
                        next_obs_ = self._vec_normalize_env.unnormalize_obs(next_obs_)
                    # Replace next obs for the correct envs
                    for key in next_obs.keys():
                        next_obs[key][i] = next_obs_[key]
                else:
                    next_obs[i] = infos[i]["terminal_observation"]
                    # VecNormalize normalizes the terminal observation
                    if self._vec_normalize_env is not None:
                        next_obs[i] = self._vec_normalize_env.unnormalize_obs(next_obs[i, :])
        
        if np.random.random() < self.p:
            
            true_replay_buffer.add(
                self._last_original_obs,  # type: ignore[arg-type]
                next_obs,  # type: ignore[arg-type]
                buffer_action,
                reward_,
                dones,
                infos,
            )

        elif unlabeled_replay_buffer is not None: # We add into pseudo-labels in case we have pseudo-buffer
            
            unlabeled_replay_buffer.add(
                self._last_original_obs,  # type: ignore[arg-type]
                next_obs,  # type: ignore[arg-type]
                buffer_action,
                reward_,
                dones,
                infos,
            )

        self._last_obs = new_obs
        # Save the unnormalized observation
        if self._vec_normalize_env is not None:
            self._last_original_obs = new_obs_

    def collect_rollouts(
        self,
        env: VecEnv,
        callback: BaseCallback,
        train_freq: TrainFreq,
        replay_buffer: ReplayBuffer,
        action_noise: Optional[ActionNoise] = None,
        learning_starts: int = 0,
        log_interval: Optional[int] = None,
        unlabeled_replay_buffer: ReplayBuffer = None,
    ) -> RolloutReturn:
        """
        Collect experiences and store them into a ``ReplayBuffer``.

        :param env: The training environment
        :param callback: Callback that will be called at each step
            (and at the beginning and end of the rollout)
        :param train_freq: How much experience to collect
            by doing rollouts of current policy.
            Either ``TrainFreq(<n>, TrainFrequencyUnit.STEP)``
            or ``TrainFreq(<n>, TrainFrequencyUnit.EPISODE)``
            with ``<n>`` being an integer greater than 0.
        :param action_noise: Action noise that will be used for exploration
            Required for deterministic policy (e.g. TD3). This can also be used
            in addition to the stochastic policy for SAC.
        :param learning_starts: Number of steps before learning for the warm-up phase.
        :param replay_buffer:
        :param log_interval: Log data every ``log_interval`` episodes
        :return:
        """
        # Switch to eval mode (this affects batch norm / dropout)
        self.policy.set_training_mode(False)

        num_collected_steps, num_collected_episodes = 0, 0

        assert isinstance(env, VecEnv), "You must pass a VecEnv"
        assert train_freq.frequency > 0, "Should at least collect one step or episode."

        if env.num_envs > 1:
            assert train_freq.unit == TrainFrequencyUnit.STEP, "You must use only one env when doing episodic training."

        if self.use_sde:
            self.actor.reset_noise(env.num_envs)

        callback.on_rollout_start()
        continue_training = True

        while should_collect_more_steps(train_freq, num_collected_steps, num_collected_episodes):
            if self.use_sde and self.sde_sample_freq > 0 and num_collected_steps % self.sde_sample_freq == 0:
                # Sample a new noise matrix
                self.actor.reset_noise(env.num_envs)

            " MAIN LOOP. Select action, step"
            # Select action randomly or according to policy
            actions, buffer_actions = self._sample_action(learning_starts, action_noise, env.num_envs)
            
            # Rescale and perform action
            new_obs, rewards, dones, infos = env.step(actions)

            self.num_timesteps += env.num_envs
            num_collected_steps += 1

            # Give access to local variables
            callback.update_locals(locals())
            # Only stop training if return value is False, not when it is None.
            if not callback.on_step():
                return RolloutReturn(num_collected_steps * env.num_envs, num_collected_episodes, continue_training=False)

            # Retrieve reward and episode length if using Monitor wrapper
            self._update_info_buffer(infos, dones)

            # Store data in replay buffer (normalized action and unnormalized observation)
            # Modification should be inside, since there can be multiple envs in parallel.  
            # Here it is. 
            self._store_transition(replay_buffer, buffer_actions, new_obs, rewards, dones, infos, unlabeled_replay_buffer)  # type: ignore[arg-type]

            self._logger.record("buffer/num_labeled_rewards", self.replay_buffer.size())
            if self.pseudo_mode and self.p < 1: self._logger.record("buffer/num_unlabeled_rewards", self.unlabeled_replay_buffer.size())

            self._update_current_progress_remaining(self.num_timesteps, self._total_timesteps)

            # For DQN, check if the target network should be updated
            # and update the exploration schedule
            # For SAC/TD3, the update is dones as the same time as the gradient update
            # see https://github.com/hill-a/stable-baselines/issues/900
            self._on_step()

            for idx, done in enumerate(dones):
                if done:
                    # Update stats
                    num_collected_episodes += 1
                    self._episode_num += 1

                    if action_noise is not None:
                        kwargs = dict(indices=[idx]) if env.num_envs > 1 else {}
                        action_noise.reset(**kwargs)

                    # Log training infos
                    if log_interval is not None and self._episode_num % log_interval == 0:
                        self._dump_logs()
        callback.on_rollout_end()

        return RolloutReturn(num_collected_steps * env.num_envs, num_collected_episodes, continue_training)
