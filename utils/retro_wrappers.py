import numpy as np
from collections import deque
import gym
from gym import spaces
import cv2
cv2.ocl.setUseOpenCL(False)

from retro.examples.discretizer import SonicDiscretizer
from utils.action_wrappers import SMarioKartDiscretizer, MegaManDiscretizer, FZeroDiscretizer

class NoopResetEnv(gym.Wrapper):
    def __init__(self, env, noop_max=30):
        """Sample initial states by taking random number of no-ops on reset.
        No-op is assumed to be action 0.
        """
        gym.Wrapper.__init__(self, env)
        self.noop_max = noop_max
        self.override_num_noops = None
        self.noop_action = 0
        assert env.unwrapped.get_action_meanings()[0] == 'NOOP'

    def reset(self, **kwargs):
        """ Do no-op action for a number of steps in [1, noop_max]."""
        self.env.reset(**kwargs)
        if self.override_num_noops is not None:
            noops = self.override_num_noops
        else:
            noops = self.unwrapped.np_random.randint(1, self.noop_max + 1) #pylint: disable=E1101
        assert noops > 0
        obs = None
        for _ in range(noops):
            obs, _, done, _ = self.env.step(self.noop_action)
            if done:
                obs = self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)

class FireResetEnv(gym.Wrapper):
    def __init__(self, env):
        """Take action on reset for environments that are fixed until firing."""
        gym.Wrapper.__init__(self, env)
        assert env.unwrapped.get_action_meanings()[1] == 'FIRE'
        assert len(env.unwrapped.get_action_meanings()) >= 3

    def reset(self, **kwargs):
        self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(1)
        if done:
            self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(2)
        if done:
            self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)

class EpisodicLifeEnv(gym.Wrapper):
    def __init__(self, env):
        """Make end-of-life == end-of-episode, but only reset on true game over.
        Done by DeepMind for the DQN and co. since it helps value estimation.
        """
        gym.Wrapper.__init__(self, env)
        self.lives = 0
        self.was_real_done  = True

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        self.was_real_done = done
        # check current lives, make loss of life terminal,
        # then update lives to handle bonus lives
        lives = self.env.unwrapped.ale.lives()
        if lives < self.lives and lives > 0:
            # for Qbert sometimes we stay in lives == 0 condition for a few frames
            # so it's important to keep lives > 0, so that we only reset once
            # the environment advertises done.
            done = True
        self.lives = lives
        return obs, reward, done, info

    def reset(self, **kwargs):
        """Reset only when lives are exhausted.
        This way all states are still reachable even though lives are episodic,
        and the learner need not know about any of this behind-the-scenes.
        """
        if self.was_real_done:
            obs = self.env.reset(**kwargs)
        else:
            # no-op step to advance from terminal/lost life state
            obs, _, _, _ = self.env.step(0)
        self.lives = self.env.unwrapped.ale.lives()
        return obs

class MaxAndSkipEnv(gym.Wrapper):
    def __init__(self, env, skip=4):
        """Return only every `skip`-th frame"""
        gym.Wrapper.__init__(self, env)
        # most recent raw observations (for max pooling across time steps)
        self._obs_buffer = np.zeros((2,)+env.observation_space.shape, dtype=np.uint8)
        self._skip       = skip

    def step(self, action):
        """Repeat action, sum reward, and max over last observations."""
        total_reward = 0.0
        done = None
        for i in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            if i == self._skip - 2: self._obs_buffer[0] = obs
            if i == self._skip - 1: self._obs_buffer[1] = obs
            total_reward += reward
            if done:
                break
        # Note that the observation on the done=True frame
        # doesn't matter
        max_frame = self._obs_buffer.max(axis=0)

        return max_frame, total_reward, done, info

    def reset(self, **kwargs):
        return self.env.reset(**kwargs)

class ClipRewardEnv(gym.RewardWrapper):
    def __init__(self, env):
        gym.RewardWrapper.__init__(self, env)

    def reward(self, reward):
        """Bin reward to {+1, 0, -1} by its sign."""
        return np.sign(reward)


class WarpFrame(gym.ObservationWrapper):
    def __init__(self, env, width=84, height=84, grayscale=True, dict_space_key=None):
        """
        Warp frames to 84x84 as done in the Nature paper and later work.
        If the environment uses dictionary observations, `dict_space_key` can be specified which indicates which
        observation should be warped.
        """
        super().__init__(env)
        self._width = width
        self._height = height
        self._grayscale = grayscale
        self._key = dict_space_key
        if self._grayscale:
            num_colors = 1
        else:
            num_colors = 3

        new_space = gym.spaces.Box(
            low=0,
            high=255,
            shape=(self._height, self._width, num_colors),
            dtype=np.uint8,
        )
        if self._key is None:
            original_space = self.observation_space
            self.observation_space = new_space
        else:
            original_space = self.observation_space.spaces[self._key]
            self.observation_space.spaces[self._key] = new_space
        assert original_space.dtype == np.uint8 and len(original_space.shape) == 3

    def observation(self, obs):
        if self._key is None:
            frame = obs
        else:
            frame = obs[self._key]

        if self._grayscale:
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        frame = cv2.resize(
            frame, (self._width, self._height), interpolation=cv2.INTER_AREA
        )
        if self._grayscale:
            frame = np.expand_dims(frame, -1)

        if self._key is None:
            obs = frame
        else:
            obs = obs.copy()
            obs[self._key] = frame
        return obs

class WarpCutFrame(gym.ObservationWrapper):
    '''Esse wrapper serve para cortar a imagem
        x e y sao as porcentagens de inicio do corte em relacao a imagem original
        width e height sao as porcentagens do tamanho da imagem desejados em 
        relacao a imagem original
        OBS: esse codigo foi planejado somente para ambientes de imagens
    '''
    def __init__(self, env, width=1, height=0.5, x=0, y=0):
        super().__init__(env)
        self._height = int(env.observation_space.shape[0]*height)
        self._width = int(env.observation_space.shape[1]*width)
        self.y = int(env.observation_space.shape[0]*y)
        self.x = int(env.observation_space.shape[1]*x)

        new_space = gym.spaces.Box(
            low=0,
            high=255,
            shape=(self._height,self._width, env.observation_space.shape[2]),
            dtype=np.uint8,
        )
        
        original_space = self.observation_space
        self.observation_space = new_space
        assert original_space.dtype == np.uint8 and len(original_space.shape) == 3

    def observation(self, obs):
    
        frame = obs
        frame = frame[self.y: self.y + self._height, self.x: self.x + self._width]

        obs = frame
        return obs

class FrameStack(gym.Wrapper):
    def __init__(self, env, k):
        """Stack k last frames.
        Returns lazy array, which is much more memory efficient.
        See Also
        --------
        baselines.common.atari_wrappers.LazyFrames
        """
        gym.Wrapper.__init__(self, env)
        self.k = k
        self.frames = deque([], maxlen=k)
        shp = env.observation_space.shape
        self.observation_space = spaces.Box(low=0, high=255, shape=(shp[:-1] + (shp[-1] * k,)), dtype=env.observation_space.dtype)

    def reset(self):
        ob = self.env.reset()
        for _ in range(self.k):
            self.frames.append(ob)
        return self._get_ob()

    def step(self, action):
        ob, reward, done, info = self.env.step(action)
        self.frames.append(ob)
        return self._get_ob(), reward, done, info

    def _get_ob(self):
        assert len(self.frames) == self.k
        return LazyFrames(list(self.frames))

class ScaledFloatFrame(gym.ObservationWrapper):
    def __init__(self, env):
        gym.ObservationWrapper.__init__(self, env)
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=env.observation_space.shape, dtype=np.float32)

    def observation(self, observation):
        # careful! This undoes the memory optimization, use
        # with smaller replay buffers only.
        return np.array(observation).astype(np.float32) / 255.0

class LazyFrames(object):
    def __init__(self, frames):
        """This object ensures that common frames between the observations are only stored once.
        It exists purely to optimize memory usage which can be huge for DQN's 1M frames replay
        buffers.
        This object should only be converted to numpy array before being passed to the model.
        You'd not believe how complex the previous solution was."""
        self._frames = frames
        self._out = None

    def _force(self):
        if self._out is None:
            self._out = np.concatenate(self._frames, axis=-1)
            self._frames = None
        return self._out

    def __array__(self, dtype=None):
        out = self._force()
        if dtype is not None:
            out = out.astype(dtype)
        return out

    def __len__(self):
        return len(self._force())

    def __getitem__(self, i):
        return self._force()[i]

    def count(self):
        frames = self._force()
        return frames.shape[frames.ndim - 1]

    def frame(self, i):
        return self._force()[..., i]

class TimeLimitWrapper(gym.Wrapper):
    def __init__(self, env, max_episode_steps=2500):
        super(TimeLimitWrapper, self).__init__(env)
        self._max_episode_steps = max_episode_steps
        self._elapsed_steps = 0

    def step(self, ac):
        observation, reward, done, info = self.env.step(ac)
        self._elapsed_steps += 1
        if self._elapsed_steps >= self._max_episode_steps:
            done = True
            info['TimeLimit.truncated'] = True
        return observation, reward, done, info

    def reset(self, **kwargs):
        self._elapsed_steps = 0
        return self.env.reset(**kwargs)

class ObsReshape(gym.ObservationWrapper):
    def __init__(self, env):
        gym.ObservationWrapper.__init__(self, env)
        new_shape = (env.observation_space.shape[2], env.observation_space.shape[1], env.observation_space.shape[0])
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=new_shape, dtype=np.float32)

    def observation(self, observation):
        observation = np.swapaxes(observation, -3, -1)
        observation = np.swapaxes(observation, -1, -2)
        return observation

class DiscountRewardEnv(gym.RewardWrapper):
    def __init__(self, env, discount=0.005):
        gym.RewardWrapper.__init__(self, env)
        self.discount = discount

    def reward(self, reward):
        return (reward - self.discount)

class PenalizeDoneWrapper(gym.Wrapper):
  def __init__(self, env, penalty=1):
    super(PenalizeDoneWrapper, self).__init__(env)
    self.penalty = penalty

  def step(self, action):
    obs, reward, done, info = self.env.step(action)
    if done:
        reward -= self.penalty
    return obs, reward, done, info

class MultipleStates(gym.Wrapper):
    """This wrapper randomly loads a state listed in the state_names
    upon reset"""
    def __init__(self, env, state_names = -1):
        gym.Wrapper.__init__(self, env)
        self.state_names = state_names
        
    def reset(self):
        if type(self.state_names) == list:
            state = np.random.choice(self.state_names)
            self.env.load_state(state)
        return self.env.reset()

def wrap_retro(env, transpose=True):
    """Configure environment for Retro environment."""
    env = MaxAndSkipEnv(env, skip=4)
    env = WarpFrame(env)
    env = FrameStack(env, 4)
    env = ScaledFloatFrame(env)
    if transpose:
        env = ObsReshape(env)
    env = SonicDiscretizer(env)
    return env

def wrap_mario_kart(env, transpose=True):
    """Configure environment for Mario Kart environment."""
    env = MaxAndSkipEnv(env, skip=4)
    env = WarpCutFrame(env)
    env = WarpFrame(env)
    env = FrameStack(env, 4)
    env = ScaledFloatFrame(env)
    if transpose:
        env = ObsReshape(env)
    env = TimeLimitWrapper(env)
    env = PenalizeDoneWrapper(env)
    env = SMarioKartDiscretizer(env)
    return env

def wrap_fzero(env, transpose=True):
    """Configure environment for F-Zero environment."""
    env = MaxAndSkipEnv(env, skip=4)
    env = WarpFrame(env)
    env = FrameStack(env, 4)
    env = ScaledFloatFrame(env)
    if transpose:
        env = ObsReshape(env)
    env = FZeroDiscretizer(env)
    return env

def wrap_megaman(env, transpose=True):
    """Configure environment for MegaMan 2 environment."""
    env = MaxAndSkipEnv(env, skip=4)
    env = WarpFrame(env)
    env = FrameStack(env, 4)
    env = ScaledFloatFrame(env)
    if transpose:
        env = ObsReshape(env)
    env = MegaManDiscretizer(env)
    return env

def get_wrapper(game):
    wrapper_dict = {"SuperMarioKart-Snes": wrap_mario_kart,
                    "FZero-Snes": wrap_fzero,
                    "MegaMan2-Nes": wrap_megaman}

    return wrapper_dict.get(game, wrap_retro)