import io
import configparser
import random
import sys

import gym
from gym import spaces
from gym.utils import seeding
import numpy as np

from .agent import *
from .create_map import CreateMap
from gym_cap.envs import const

"""
Requires that all units initially exist in home zone.
"""


class CapEnv(gym.Env):
    metadata = {
        "render.modes": ["fast", "human", 'rgb_array'],
        'video.frames_per_second' : 50
    }

    ACTION = ["X", "N", "E", "S", "W"]

    def __init__(self, map_size=20, mode="random"):
        """

        Parameters
        ----------
        self    : object
            CapEnv object
        """
        self.seed()
        self.viewer = None
        self._parse_config()

        self.reset(map_size, mode=mode)

    def _parse_config(self, config_path=None):
        # Set configuration constants
        # If config path is specified, read the file
        # Default values are set in const.py file

        config_param = { # Configurable parameters
                'elements': ['NUM_BLUE', 'NUM_RED', 'NUM_UAV', 'NUM_GRAY'],
                'settings': ['RL_SUGGESTIONS', 'STOCH_TRANSITIONS',
                        'STOCH_ATTACK', 'STOCH_ZONES', 'RED_PARTIAL', 'BLUE_PARTIAL']
            }
        config_datatype = {
                'elements': [int, int, int ,int],
                'settings': [bool, bool, bool, bool, bool, bool]
            }

        if config_path is None:
            # Default configuration
            get = lambda key, name: getattr(const, name)
        else:
            # Custom configuration
            config = configparser.ConfigParser()
            config.read(config_path)
            get = lambda key, name: config.get(key, name, fallback=getattr(const, name))

        try:
            # Set environment attributes
            for section in config_param:
                for name, datatype in zip(config_param[section], config_datatype[section]):
                    setattr(self, name, datatype(get(section, name)))
        except Exception as e:
            print(e)
            print('Configuration import fails: recheck whether all config variables are included')
            exit()

    def reset(self, map_size=None, mode="random", policy_blue=None, policy_red=None, custom_board=None, config_path=None):
        """
        Resets the game

        :param map_size: Size of the map
        :param mode: Action generation mode
        :return: void

        """

        # ASSERTIONS


        # WARNINGS
        if config_path is not None and custom_board is not None:
            print('Custom configuration path is specified, but the custom board is given. Configuration will be ignored.')

        # Pull default values
        if config_path is not None:
            self._parse_config(config_path)
        if map_size is None:
            map_size = self.map_size[0]
        if policy_blue is not None:
            self.policy_blue = policy_blue
        if policy_red is not None:
            self.policy_red = policy_red

        # Store Arguments
        self.mode = mode

        if self.STOCH_ATTACK:
            self.interaction = self._interaction_stoch
        else:
            self.interaction = self._interaction_determ

        if custom_board is not None:
            # Reset using pre-written custom board
            try:
                custom_map = np.loadtxt(custom_board, dtype = int, delimiter = " ")
            except OSError as e:
                raise e(f'File name {custom_board} failed to open')
                exit() 

            self._env, self.team_home, map_obj = CreateMap.set_custom_map(custom_map)
        else:
            map_obj = [self.NUM_BLUE, self.NUM_UAV, self.NUM_RED, self.NUM_UAV, self.NUM_GRAY]
            self._env, self.team_home = CreateMap.gen_map('map',
                    map_size, rand_zones=self.STOCH_ZONES, np_random=self.np_random, map_obj=map_obj)

        self.map_size = (len(self._env), len(self._env[0]))
        self.action_space = spaces.Discrete(len(self.ACTION) ** (map_obj[0] + map_obj[1]))
        if map_obj[2] == 0:
            self.mode = "sandbox"

        if policy_blue is not None: self.policy_blue = policy_blue
        if policy_red is not None: self.policy_red = policy_red
            
        self.team_blue, self.team_red = self._map_to_list(self._env, self.team_home)

        self.create_observation_space()

        self.blue_win = False
        self.red_win = False
        self.red_flag = False
        self.blue_flag = False
        self.red_die = False
        self.blue_die = False

        # Necessary for human mode
        self.first = True

        return self.get_obs_blue

    def _map_to_list(self, complete_map, static_map):
        """
        From given map, it generates objects of agents and push them to list

        self            : objects
        complete_map    : 2d numpy array
        static_map      : 2d numpy array
        """
        team_blue = []
        team_red = []

        for y in range(len(complete_map)):
            for x in range(len(complete_map[0])):
                if complete_map[x][y] == TEAM1_UGV:
                    cur_ent = GroundVehicle([x, y], static_map, TEAM1_BACKGROUND)
                    team_blue.append(cur_ent)
                elif complete_map[x][y] == TEAM1_UAV:
                    cur_ent = AerialVehicle([x, y], static_map, TEAM1_BACKGROUND)
                    team_blue.insert(0, cur_ent)
                elif complete_map[x][y] == TEAM2_UGV:
                    cur_ent = GroundVehicle([x, y], static_map, TEAM2_BACKGROUND)
                    team_red.append(cur_ent)
                elif complete_map[x][y] == TEAM2_UAV:
                    cur_ent = AerialVehicle([x, y], static_map, TEAM2_BACKGROUND)
                    team_red.insert(0, cur_ent)

        return team_blue, team_red

    def create_reward(self):
        """
        Range (-100, 100)

        Parameters
        ----------
        self    : object
            CapEnv object
        """

        if self.blue_win:
            return 100
        if self.red_win:
            return -100

        # Dead enemy team gives .5/total units for each dead unit
        reward = 0
        red_alive = sum([entity.isAlive for entity in self.team_red])
        blue_alive = sum([entity.isAlive for entity in self.team_blue])
        reward += 50.0 * red_alive / TEAM2_UGV
        reward -= 50.0 * blue_alive / TEAM1_UGV

        return reward

    def create_observation_space(self):
        """
        Creates the observation space in self.observation_space

        Parameters
        ----------
        self    : object
            CapEnv object
        team    : int
            Team to create obs space for
        """

        #self.observation_space_blue = np.full_like(self._env, -1)
        self.observation_space_blue = np.empty(self._env.shape)
        self.observation_space_blue[:] = -1
        for agent in self.team_blue:
            if not agent.isAlive:
                continue
            loc = agent.get_loc()
            for i in range(-agent.range, agent.range + 1):
                for j in range(-agent.range, agent.range + 1):
                    locx, locy = i + loc[0], j + loc[1]
                    if (i * i + j * j <= agent.range ** 2) and \
                            not (locx < 0 or locx > self.map_size[0] - 1) and \
                            not (locy < 0 or locy > self.map_size[1] - 1):
                        self.observation_space_blue[locx][locy] = self._env[locx][locy]

        #self.observation_space_red = np.full_like(self._env, -1)
        self.observation_space_red= np.empty(self._env.shape)
        self.observation_space_red[:] = -1
        for agent in self.team_red:
            if not agent.isAlive:
                continue
            loc = agent.get_loc()
            for i in range(-agent.range, agent.range + 1):
                for j in range(-agent.range, agent.range + 1):
                    locx, locy = i + loc[0], j + loc[1]
                    if (i * i + j * j <= agent.range ** 2) and \
                            not (locx < 0 or locx > self.map_size[0] - 1) and \
                            not (locy < 0 or locy > self.map_size[1] - 1):
                        self.observation_space_red[locx][locy] = self._env[locx][locy]


        # TODO need to be added observation for grey team
        # self.observation_space_grey = np.full_like(self._env, -1)

    @property
    def get_full_state(self):
        return np.copy(self._env)

    @property
    def get_team_blue(self):
        return np.copy(self.team_blue)

    @property
    def get_team_red(self):
        return np.copy(self.team_red)

    @property
    def get_team_grey(self):
        return np.copy(self.team_grey)

    @property
    def get_map(self):
        return np.copy(self.team_home)

    @property
    def observation_space(self):
        return self.get_obs_blue() 

    @property
    def get_obs_blue(self):
        if self.BLUE_PARTIAL:
            return np.copy(self.observation_space_blue)
        else:
            return self.get_full_state

    @property
    def get_obs_red(self):
        if self.RED_PARTIAL:
            red_view = np.copy(self.observation_space_red)
        else:
            red_view = self.get_full_state

        # Change red's perspective same as blue
        swap = [
            (TEAM1_BACKGROUND, TEAM2_BACKGROUND), # BACKGROUND
            (TEAM1_UGV, TEAM2_UGV),               # UGV
            (TEAM1_UAV, TEAM2_UAV),               # UAV
            (TEAM1_FLAG, TEAM2_FLAG),             # FLAG
        ]

        for a, b in swap:
            index_a = np.where(red_view==a)
            red_view[red_view==b] = a
            red_view[index_a] = b

        return red_view

    @property
    def get_obs_grey(self):
        return np.copy(self.observation_space_grey)


    def _interaction_determ(self, entity):
        """
        Checks if a unit is dead

        Parameters
        ----------
        self    : object
            CapEnv object
        entity_num  : int
            Represents where in the unit list is the unit to move
        team    : int
            Represents which team the unit belongs to
        """
        loc = entity.get_loc()
        cur_range = entity.a_range
        for x in range(-cur_range, cur_range + 1):
            for y in range(-cur_range, cur_range + 1):
                locx, locy = x + loc[0], y + loc[1]
                if (x * x + y * y <= cur_range ** 2) and \
                        not (locx < 0 or locx > self.map_size[0] - 1) and \
                        not (locy < 0 or locy > self.map_size[1] - 1):
                    if entity.team == TEAM1_BACKGROUND and self._env[locx][locy] == TEAM2_UGV:
                        if self.team_home[loc] == TEAM2_BACKGROUND:
                            entity.isAlive = False
                            self._env[loc] = DEAD
                            break
                    elif entity.team == TEAM2_BACKGROUND and self._env[locx][locy] == TEAM1_UGV:
                        if self.team_home[loc] == TEAM1_BACKGROUND:
                            entity.isAlive = False
                            self._env[loc] = DEAD
                            break

    def _interaction_stoch(self, entity):
        """
        Checks if a unit is dead

        Parameters
        ----------
        self    : object
            CapEnv object
        entity_num  : int
            Represents where in the unit list is the unit to move
        team    : int
            Represents which team the unit belongs to
        """
        loc = entity.get_loc()
        cur_range = entity.a_range
        n_friends = 0
        n_enemies = 0
        flag = False
        if entity.team == self.team_home[loc]:
            n_friends += 1
        else:
            n_enemies += 1

        for x in range(-cur_range, cur_range + 1):
            for y in range(-cur_range, cur_range + 1):
                locx, locy = x + loc[0], y + loc[1]
                if (x * x + y * y <= cur_range ** 2) and \
                        not (locx < 0 or locx > self.map_size[0] - 1) and \
                        not (locy < 0 or locy > self.map_size[1] - 1):
                    if entity.team == TEAM1_BACKGROUND and self._env[locx][locy] == TEAM2_UGV:
                        n_enemies += 1
                        flag = True
                    elif entity.team == TEAM2_BACKGROUND and self._env[locx][locy] == TEAM1_UGV:
                        n_enemies += 1
                        flag = True
                    elif entity.team == TEAM1_BACKGROUND and self._env[locx][locy] == TEAM1_UGV:
                        n_friends += 1
                    elif entity.team == TEAM2_BACKGROUND and self._env[locx][locy] == TEAM2_UGV:
                        n_friends += 1
        if flag and self.np_random.rand() > n_friends/(n_friends + n_enemies):

            entity.isAlive = False
            self._env[loc] = DEAD

    def seed(self, seed=None):
        """
        todo docs still

        Parameters
        ----------
        self    : object
            CapEnv object
        """
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, entities_action=None, cur_suggestions=None):
        """
        Takes one step in the cap the flag game



        :param
            entities_action: contains actions for entity 1-n
            cur_suggestions: suggestions from rl to human
        :return:
            state    : object
            CapEnv object
            reward  : float
            float containing the reward for the given action
            isDone  : bool
            decides if the game is over
            info    :
        """

        move_list = []

        # Get actions from uploaded policies
        try:
            move_list_red = self.policy_red.gen_action(self.team_red,self.get_obs_red,free_map=self.team_home)
        except:
            print("No valid policy for red team")
            exit()

        if entities_action is None:
            try:
                move_list_blue = self.policy_blue.gen_action(self.team_blue,self.get_obs_blue,free_map=self.team_home)
            except Exception as e:
                print(e)
                print("No valid policy for blue team and no actions provided")
                exit()
        elif type(entities_action) is int:
            if entities_action >= len(self.ACTION) ** (self.NUM_BLUE + self.NUM_UAV):
                sys.exit("ERROR: You entered too many moves. \
                         There are " + str(self.NUM_BLUE + self.NUM_UAV) + " entities.")
            while len(move_list) < (self.NUM_BLUE + self.NUM_UAV):
                move_list_blue.append(entities_action % 5)
                entities_action = int(entities_action / 5)
        else:
            if len(entities_action) > self.NUM_BLUE + self.NUM_UAV:
                sys.exit("ERROR: You entered too many moves. \
                         There are " + str(self.NUM_BLUE + self.NUM_UAV) + " entities.")
            move_list_blue = entities_action


        # Move team1
        for idx, act in enumerate(move_list_blue):
            if self.STOCH_TRANSITIONS and self.np_random.rand() < 0.1:
                act = self.np_random.randint(0,len(self.ACTION))
            self.team_blue[idx].move(self.ACTION[act], self._env, self.team_home)

        # Move team2
        for idx, act in enumerate(move_list_red):
            if self.STOCH_TRANSITIONS and self.np_random.rand() < 0.1:
                act = self.np_random.randint(0,len(self.ACTION))
            self.team_red[idx].move(self.ACTION[act], self._env, self.team_home)


        # Check for dead
        for entity in self.team_blue + self.team_red:
            if entity.air or not entity.isAlive:
                continue
            self.interaction(entity)

        # Check win and lose conditions
        has_alive_entity = False
        for i in self.team_red:
            if i.isAlive and not i.air:
                has_alive_entity = True
                locx, locy = i.get_loc()
                if self.team_home[locx][locy] == TEAM1_FLAG:  # TEAM 1 == BLUE
                    self.red_win = True
                    self.blue_flag = True
                    
        # TODO Change last condition for multi agent model
        if not has_alive_entity and self.mode != "sandbox" and self.mode != "human_blue":
            self.blue_win = True
            self.red_die = True

        has_alive_entity = False
        for i in self.team_blue:
            if i.isAlive and not i.air:
                has_alive_entity = True
                locx, locy = i.get_loc()
                if self.team_home[locx][locy] == TEAM2_FLAG:
                    self.blue_win = True
                    self.red_flag = True
                    
        if not has_alive_entity:
            self.red_win = True
            self.blue_die = True

        reward = self.create_reward()

        self.create_observation_space()

        isDone = self.red_win or self.blue_win
        info = {}

        return self.get_full_state, reward, isDone, info

    def render(self, mode='human'):
        """
        Renders the screen options="obs, env"

        Parameters
        ----------
        self    : object
            CapEnv object
        mode    : string
            Defines what will be rendered
        """
        SCREEN_W = 600
        SCREEN_H = 600

        if self.viewer is None:
            from gym.envs.classic_control import rendering
            self.viewer = rendering.Viewer(SCREEN_W, SCREEN_H)
            self.viewer.set_bounds(0, SCREEN_W, 0, SCREEN_H)

        self.viewer.draw_polygon([(0, 0), (SCREEN_W, 0), (SCREEN_W, SCREEN_H), (0, SCREEN_H)], color=(0, 0, 0))

        self._env_render(self.team_home,
                        [10, 10], [SCREEN_W//2-10, SCREEN_H//2-10])
        self._env_render(self.observation_space_blue,
                        [10+SCREEN_W//2, 10], [SCREEN_W//2-10, SCREEN_H//2-10])
        self._env_render(self.observation_space_red,
                        [10+SCREEN_W//2, 10+SCREEN_H//2], [SCREEN_W//2-10, SCREEN_H//2-10])
        self._env_render(self._env,
                        [10, 10+SCREEN_H//2], [SCREEN_W//2-10, SCREEN_H//2-10])

        return self.viewer.render(return_rgb_array = mode=='rgb_array')

    def _env_render(self, env, rend_loc, rend_size):
        map_h = len(env[0])
        map_w = len(env)

        tile_w = rend_size[0] / len(env)
        tile_h = rend_size[1] / len(env[0])

        for y in range(map_h):
            for x in range(map_w):
                locx, locy = rend_loc
                locx += x * tile_w
                locy += y * tile_h
                cur_color = np.divide(COLOR_DICT[env[x][y]], 255.0)
                self.viewer.draw_polygon([
                    (locx, locy),
                    (locx + tile_w, locy),
                    (locx + tile_w, locy + tile_h),
                    (locx, locy + tile_h)], color=cur_color)

                if env[x][y] == TEAM1_UAV or env[x][y] == TEAM2_UAV:
                    self.viewer.draw_polyline([
                        (locx, locy),
                        (locx + tile_w, locy + tile_h)],
                        color=(0,0,0), linewidth=2)
                    self.viewer.draw_polyline([
                        (locx + tile_w, locy),
                        (locx, locy + tile_h)],
                        color=(0,0,0), linewidth=2)#col * tile_w, row * tile_h

    def close(self):
        if self.viewer: self.viewer.close()


    # def quit_game(self):
    #     if self.viewer is not None:
    #         self.viewer.close()
    #         self.viewer = None


# Different environment sizes and modes
# Random modes
class CapEnvGenerate(CapEnv):
    def __init__(self):
        super(CapEnvGenerate, self).__init__(map_size=20)
