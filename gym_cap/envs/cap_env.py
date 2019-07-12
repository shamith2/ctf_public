import __future__

import io
try:
    import configparser
except ImportError:
    import ConfigParser as configparser
    
import random
import sys
import traceback

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

    def __init__(self, map_size=20, mode="random", **kwargs):
        """

        Parameters
        ----------
        self    : object
            CapEnv object
        """
        self.seed()
        self.viewer = None

        self.blue_memory = np.empty((map_size, map_size), dtype=int)
        self.red_memory = np.empty((map_size, map_size), dtype=int)

        self._parse_config()

        self._policy_blue = None
        self._policy_red = None

        self._blue_trajectory = []
        self._red_trajectory = []

        self.reset(map_size, mode=mode,
                policy_blue=kwargs.get('policy_blue', None),
                policy_red=kwargs.get('policy_red', None),
                custom_board=kwargs.get('custom_board', None),
                config_path=kwargs.get('config_path', None),
            )

    def _parse_config(self, config_path=None):
        # Set configuration constants
        # If config path is specified, read the file
        # Default values are set in const.py file

        config_param = { # Configurable parameters
                'elements': ['NUM_BLUE', 'NUM_RED', 'NUM_UAV', 'NUM_GRAY'],
                'control': ['CONTROL_ALL'],
                'communication': ['COM_GROUND', 'COM_AIR', 'COM_DISTANCE', 'COM_FREQUENCY'],
                'memory': ['INDIV_MEMORY', 'TEAM_MEMORY', 'RENDER_INDIV_MEMORY', 'RENDER_TEAM_MEMORY'],
                'settings': ['RL_SUGGESTIONS', 'STOCH_TRANSITIONS', 'STOCH_TRANSITIONS_EPS',
                        'STOCH_ATTACK', 'STOCH_ATTACK_BIAS', 'STOCH_ZONES', 'RED_PARTIAL', 'BLUE_PARTIAL']
            }
        config_datatype = {
                'elements': [int, int, int ,int],
                'control': [bool],
                'communication': [bool, bool, int, float],
                'memory': [str, str, bool, bool],
                'settings': [bool, bool, float,
                        bool, int, bool, bool, bool]
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
                    value = get(section, name)
                    if datatype is bool:
                        if type(value) == str:
                            value = True if value == 'True' else False
                    elif datatype is int or datatype is float:
                        value = datatype(value)
                    setattr(self, name, value)
        except Exception as e:
            print(e)
            raise Exception('Configuration import fails: recheck whether all config variables are included')

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
            print('Custom configuration path is specified, but the custom board is given. Some configuration will be ignored.')

        # STORE ARGUMENTS
        self.mode = mode

        # LOAD DEFAULT PARAMETERS
        if config_path is not None:
            self._parse_config(config_path)
        if map_size is None:
            map_size = self.map_size[0]

        # SET INTERACTION
        if self.STOCH_ATTACK:
            self._interaction = self._interaction_stoch
        else:
            self._interaction = self._interaction_determ

        # INITIALIZE MAP
        if custom_board is not None:
            # Reset using pre-written custom board
            custom_map = np.loadtxt(custom_board, dtype = int, delimiter = " ")

            self._env, self._static_map, map_obj, agent_locs = CreateMap.set_custom_map(custom_map)
            self.NUM_BLUE, self.NUM_UAV, self.NUM_RED, self.NUM_UAV, self.NUM_GRAY = map_obj
        else:
            map_obj = [self.NUM_BLUE, self.NUM_UAV, self.NUM_RED, self.NUM_UAV, self.NUM_GRAY]
            self._env, self._static_map, agent_locs = CreateMap.gen_map('map',
                    map_size, rand_zones=self.STOCH_ZONES, np_random=self.np_random, map_obj=map_obj)

        self.map_size = tuple(self._static_map.shape)
        self.action_space = spaces.Discrete(len(self.ACTION) ** (map_obj[0] + map_obj[1]))
        self.observation_space = Board(shape=[self.map_size[0], self.map_size[1], NUM_CHANNEL])
        if map_obj[2] == 0:
            self.mode = "sandbox"

        # INITIALIZE TEAM
        self._team_blue, self._team_red = self._construct_agents(agent_locs, self._static_map)

        # INITIALIZE POLICY
        if policy_blue is not None:
            try:
                self._policy_blue = policy_blue
            except Exception as e:
                print("Blue policy does not have Policy_gen object", e)
                raise
        if policy_red is not None:
            try:
                self._policy_red = policy_red
            except Exception as e:
                print("Red policy does not have Policy_gen object", e)
                raise

        # INITIALIZE MEMORY
        if self.TEAM_MEMORY == "fog":
            self.blue_memory[:] = const.UNKNOWN
            self.red_memory[:] = const.UNKNOWN

        if self.INDIV_MEMORY == "fog":
            for agent in self._team_blue + self._team_red:
                agent.memory[:] = const.UNKNOWN
                agent.memory_mode = "fog"

        # INITIATE POLICY
        if self._policy_blue is not None:
            self._policy_blue.initiate(self._static_map, self._team_blue)
        if self._policy_red is not None:
            self._policy_red.initiate(self._static_map, self._team_red)

        # INITIALIZE TRAJECTORY
        self._blue_trajectory = []
        self._red_trajectory = []

        self._create_observation_space()

        self.blue_win = False
        self.red_win = False
        self.red_flag_captured = False
        self.blue_flag_captured = False
        self.red_eliminated = False
        self.blue_eliminated = False

        # Necessary for human mode
        self.first = True

        return self.get_obs_blue

    def _construct_agents(self, agent_coords, static_map):
        """
        From given coordinates, it generates objects of agents and make them into the list.

        team_blue --> [air1, air2, ... , ground1, ground2, ...]
        team_red  --> [air1, air2, ... , ground1, ground2, ...]

        complete_map    : 2d numpy array
        static_map      : 2d numpy array

        """
        team_blue = []
        team_red = []

        for element, coords in agent_coords.items():
            if coords is None: continue
            for coord in coords:
                if element == TEAM1_UGV:
                    cur_ent = GroundVehicle(coord, static_map, TEAM1_BACKGROUND)
                    team_blue.append(cur_ent)
                elif element == TEAM1_UAV:
                    cur_ent = AerialVehicle(coord, static_map, TEAM1_BACKGROUND)
                    team_blue.insert(0, cur_ent)
                elif element == TEAM2_UGV:
                    cur_ent = GroundVehicle(coord, static_map, TEAM2_BACKGROUND)
                    team_red.append(cur_ent)
                elif element == TEAM2_UAV:
                    cur_ent = AerialVehicle(coord, static_map, TEAM2_BACKGROUND)
                    team_red.insert(0, cur_ent)

        return team_blue, team_red

    def _create_reward(self, mode='dense'):
        """
        Range (-100, 100)

        Parameters
        ----------
        self    : object
            CapEnv object
        """

        assert mode in ['dense', 'flag', 'combat', 'defense', 'capture']

        if mode == 'dense':
            # Dead enemy team gives .5/total units for each dead unit
            if self.blue_win:
                return 100
            if self.red_win:
                return -100
            reward = 0
            red_alive = sum([entity.isAlive for entity in self._team_red if not entity.air])
            blue_alive = sum([entity.isAlive for entity in self._team_blue if not entity.air])
            reward += 50.0 * red_alive / TEAM2_UGV
            reward -= 50.0 * blue_alive / TEAM1_UGV
            return reward
        elif mode == 'flag':
            # Flag game reward
            if self.red_flag_captured:
                return 100
            if self.blue_flag_captured:
                return -100
        elif mode == 'combat':
            # Aggressive combat game. Elliminate enemy to win
            red_alive = sum([entity.isAlive for entity in self._team_red if not entity.air])
            return 100 * red_alive / TEAM2_UGV
        elif mode == 'defense':
            # Lose reward if flag is lost.
            if self.blue_flag_captured:
                return -100
        elif mode == 'capture':
            # Reward only by capturing (sparse)
            if self.red_flag_captured:
                return 100


    def _create_observation_space(self):
        """
        Creates the observation space in self.observation_space

        Parameters
        ----------
        self    : object
            CapEnv object
        team    : int
            Team to create obs space for
        """
        in_bound = lambda x, y: (0<=x) and (x<self.map_size[0]) and (0<=y) and (y<self.map_size[0])
        in_range = lambda i, j, r: i*i + j*j <= r*r + 1e-8
        
        unknown_ch = CHANNEL[UNKNOWN]
        unknown_repr = REPRESENT[UNKNOWN] 

        if self.BLUE_PARTIAL:
            self.observation_space_blue = np.copy(self._env)
            self.observation_space_blue[:,:,unknown_ch] = unknown_repr
            for agent in self._team_blue:
                if not agent.isAlive:
                    continue
                loc = agent.get_loc()
                for i in range(-agent.range, agent.range + 1):
                    for j in range(-agent.range, agent.range + 1):
                        locx, locy = i + loc[0], j + loc[1]
                        if in_range(i,j,agent.range) and in_bound(locx, locy):
                            self.observation_space_blue[locx][locy][unknown_ch] = 0
        else:
            self.observation_space_blue = self._env

        if self.RED_PARTIAL:
            self.observation_space_red = np.copy(self._env)
            self.observation_space_red[:,:,unknown_ch] = unknown_repr
            for agent in self._team_red:
                if not agent.isAlive:
                    continue
                loc = agent.get_loc()
                for i in range(-agent.range, agent.range + 1):
                    for j in range(-agent.range, agent.range + 1):
                        locx, locy = i + loc[0], j + loc[1]
                        if in_range(i,j,agent.range) and in_bound(locx, locy):
                            self.observation_space_red[locx][locy][unknown_ch] = 0
        else:
            self.observation_space_red = self._env


        # TODO need to be added observation for grey team
        # self.observation_space_grey = np.full_like(self._env, -1)

    @property
    def get_full_state(self):
        # Return 2D representation of the state
        board = np.copy(self._static_map)
        for entities in self._team_blue+self._team_red:
            if not entities.isAlive: continue
            loc = entities.get_loc()
            if entities.team == TEAM1_BACKGROUND and entities.air:
                board[loc] = TEAM1_UAV
            elif entities.team == TEAM1_BACKGROUND and not entities.air:
                board[loc] = TEAM1_UGV
            elif entities.team == TEAM2_BACKGROUND and entities.air:
                board[loc] = TEAM2_UAV
            elif entities.team == TEAM2_BACKGROUND and not entities.air:
                board[loc] = TEAM2_UGV
        return board

    @property
    def get_team_blue(self):
        return np.copy(self._team_blue)

    @property
    def get_team_red(self):
        return np.copy(self._team_red)

    @property
    def get_team_grey(self):
        return np.copy(self.team_grey)

    @property
    def get_map(self):
        return np.copy(self._static_map)

    @property
    def get_obs_blue(self):
        return np.copy(self.observation_space_blue)

    @property
    def get_obs_red(self):
        target = self.observation_space_red
        red_view = np.copy(target)

        # Change red's perspective same as blue
        swap = [CHANNEL[TEAM1_BACKGROUND], CHANNEL[TEAM1_UGV], CHANNEL[TEAM1_UAV], CHANNEL[TEAM1_FLAG]]

        for ch in swap:
            red_view[:,:,ch] *= -1

        return red_view

    @property
    def get_obs_blue_render(self):
        board = np.copy(self._static_map)
        fog = self.observation_space_blue[:,:,CHANNEL[UNKNOWN]]
        fog_rep = REPRESENT[UNKNOWN]
        board[fog==fog_rep] = UNKNOWN
        for entities in self._team_blue+self._team_red:
            if not entities.isAlive: continue
            loc = entities.get_loc()
            if fog[loc] == fog_rep: continue
            if entities.team == TEAM1_BACKGROUND and entities.air:
                board[loc] = TEAM1_UAV
            elif entities.team == TEAM1_BACKGROUND and not entities.air:
                board[loc] = TEAM1_UGV
            elif entities.team == TEAM2_BACKGROUND and entities.air:
                board[loc] = TEAM2_UAV
            elif entities.team == TEAM2_BACKGROUND and not entities.air:
                board[loc] = TEAM2_UGV
        return board

    @property
    def get_obs_red_render(self):
        board = np.copy(self._static_map)
        fog = self.observation_space_red[:,:,CHANNEL[UNKNOWN]]
        fog_rep = REPRESENT[UNKNOWN]
        board[fog==fog_rep] = UNKNOWN
        for entities in self._team_blue+self._team_red:
            if not entities.isAlive: continue
            loc = entities.get_loc()
            if fog[loc] == fog_rep: continue
            if entities.team == TEAM1_BACKGROUND and entities.air:
                board[loc] = TEAM1_UAV
            elif entities.team == TEAM1_BACKGROUND and not entities.air:
                board[loc] = TEAM1_UGV
            elif entities.team == TEAM2_BACKGROUND and entities.air:
                board[loc] = TEAM2_UAV
            elif entities.team == TEAM2_BACKGROUND and not entities.air:
                board[loc] = TEAM2_UGV
        return board

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

        in_range = lambda i, j, r: i*i + j*j <= r*r + 1e-8

        loc = np.array(entity.get_loc())
        if self._static_map[tuple(loc)] == entity.team:
            return

        enemy_list = self._team_red if entity.team == TEAM1_BACKGROUND else self._team_blue

        for enemy in enemy_list:
            if enemy.air: continue
            if not enemy.isAlive: continue
            att_range = enemy.a_range
            enemy_loc = np.array(enemy.get_loc())

            if in_range(*(loc-enemy_loc), att_range):
                # If enemy is within attack range, declare dead
                entity.isAlive = False
                self._env[loc[0], loc[1], CHANNEL[DEAD]] = REPRESENT[DEAD]
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

        in_range = lambda i, j, r: i*i + j*j <= r*r + 1e-8

        loc = np.array(entity.get_loc())

        enemy_list = self._team_red if entity.team == TEAM1_BACKGROUND else self._team_blue
        friend_list = self._team_blue if entity.team == TEAM1_BACKGROUND else self._team_red

        n_enemies = 0
        for enemy in enemy_list:
            if enemy.air: continue
            if not enemy.isAlive: continue
            att_range = enemy.a_range
            enemy_loc = np.array(enemy.get_loc())

            if in_range(*(loc-enemy_loc), att_range):
                n_enemies += 1

        n_friends = 0
        for friend in friend_list:
            if friend.air: continue
            if not friend.isAlive: continue
            att_range = friend.a_range
            friend_loc = np.array(friend.get_loc())

            if np.all(friend_loc != loc) and in_range(*(loc-friend_loc), att_range):
                n_friends += 1

        if n_enemies > 0: # Interaction 
            # Advantage bias for being in team territory
            if entity.team == self._static_map[entity.get_loc()]:
                n_friends += self.STOCH_ATTACK_BIAS
            else:
                n_enemies += self.STOCH_ATTACK_BIAS
            if self.np_random.rand() > n_friends/(n_friends + n_enemies):
                entity.isAlive = False
                self._env[loc[0], loc[1], CHANNEL[DEAD]] = REPRESENT[DEAD]

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

    def _update_global_memory(self):
        """ 
        team memory map
        
        """
        
        l, b = self.blue_memory.shape
        for blue_agent in self._team_blue:
            b_obs = blue_agent.get_obs(env=self)
            leng, breth = b_obs.shape
            leng, breth = leng//2, breth//2
            b_coord_x, b_coord_y = blue_agent.get_loc()
            b_offset_x, b_offset_y = leng - b_coord_x, breth - b_coord_y
            b_obs = b_obs[b_offset_x: b_offset_x + l, b_offset_y: b_offset_y + b]   
            b_coord = b_obs!= const.UNKNOWN
            self.blue_memory[b_coord] = self._static_map[b_coord]
             
        l, b = self.red_memory.shape
        for red_agent in self._team_red:
            r_obs = red_agent.get_obs(env=self)
            leng, breth = r_obs.shape
            leng, breth = leng//2, breth//2
            r_coord_x, r_coord_y = red_agent.get_loc()
            r_offset_x, r_offset_y = leng - r_coord_x, breth - r_coord_y
            r_obs = r_obs[r_offset_x: r_offset_x + l, r_offset_y: r_offset_y + b]   
            r_coord = r_obs!= const.UNKNOWN
            self.red_memory[r_coord] = self._static_map[r_coord]
        
        return

    def step(self, entities_action=None, cur_suggestions=None):
        """
        Takes one step in the capture the flag game

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

        indiv_action_space = len(self.ACTION)

        if self.CONTROL_ALL:
            assert entities_action is not None, 'Under CONTROL_ALL setting, action must be specified'
            assert (type(entities_action) is list) or (type(entities_action) is np.ndarray), \
                    'CONTROLL_ALL setting requires list (or numpy array) type of action'
            assert len(entities_action) == self.NUM_BLUE+self.NUM_RED+self.NUM_UAV+self.NUM_UAV, \
                    'You entered wrong number of moves.'

            move_list_blue = entities_action[:self.NUM_UAV+self.NUM_BLUE]
            move_list_red  = entities_action[-self.NUM_UAV-self.NUM_RED:]
        else:
            # Get actions from uploaded policies
            try:
                move_list_red = self._policy_red.gen_action(self._team_red, self.get_obs_red)
            except Exception as e:
                print("No valid policy for red team", e)
                traceback.print_exc()
                exit()

            if entities_action is None:
                try:
                    move_list_blue = self._policy_blue.gen_action(self._team_blue, self.get_obs_blue)
                except Exception as e:
                    print("No valid policy for blue team and no actions provided", e)
                    traceback.print_exc()
                    exit()
            elif type(entities_action) is int:
                if entities_action >= len(self.ACTION) ** (self.NUM_BLUE + self.NUM_UAV):
                    sys.exit("ERROR: You entered too many moves. There are " + str(self.NUM_BLUE + self.NUM_UAV) + " entities.")
                move_list_blue = []
                while len(move_list_blue) < (self.NUM_BLUE + self.NUM_UAV):
                    move_list_blue.append(entities_action % indiv_action_space)
                    entities_action = int(entities_action / indiv_action_space)
            else:
                if len(entities_action) != self.NUM_BLUE + self.NUM_UAV:
                    sys.exit("ERROR: You entered wrong number of moves. There are " + str(self.NUM_BLUE + self.NUM_UAV) + " entities.")
                move_list_blue = entities_action


        # Move team1
        positions = []
        for idx, act in enumerate(move_list_blue):
            if self.STOCH_TRANSITIONS and self.np_random.rand() < self.STOCH_TRANSITIONS_EPS:
                act = self.np_random.randint(0,len(self.ACTION))
            self._team_blue[idx].move(self.ACTION[act], self._env, self._static_map)
            positions.append((self._team_blue[idx].get_loc(), self._team_blue[idx].isAlive))
        self._blue_trajectory.append(positions)

        # Move team2
        positions = []
        for idx, act in enumerate(move_list_red):
            if self.STOCH_TRANSITIONS and self.np_random.rand() < self.STOCH_TRANSITIONS_EPS:
                act = self.np_random.randint(0,len(self.ACTION))
            self._team_red[idx].move(self.ACTION[act], self._env, self._static_map)
            positions.append((self._team_red[idx].get_loc(), self._team_red[idx].isAlive))
        self._red_trajectory.append(positions)

        # Run interaction
        for entity in self._team_blue + self._team_red:
            if entity.air or not entity.isAlive:
                continue
            self._interaction(entity)

        # Check win and lose conditions
        has_alive_entity = False
        for i in self._team_red:
            if i.isAlive and not i.air:
                has_alive_entity = True
                locx, locy = i.get_loc()
                if self._static_map[locx][locy] == TEAM1_FLAG:  # TEAM 1 == BLUE
                    self.red_win = True
                    self.blue_flag_captured = True
                    
        # TODO Change last condition for multi agent model
        if not has_alive_entity and self.mode != "sandbox" and self.mode != "human_blue":
            self.blue_win = True
            self.red_eliminated = True

        has_alive_entity = False
        for i in self._team_blue:
            if i.isAlive and not i.air:
                has_alive_entity = True
                locx, locy = i.get_loc()
                if self._static_map[locx][locy] == TEAM2_FLAG:
                    self.blue_win = True
                    self.red_flag_captured = True
                    
        if not has_alive_entity:
            self.red_win = True
            self.blue_eliminated = True

        reward = self._create_reward()

        self._create_observation_space()

        isDone = self.red_win or self.blue_win
        
        # Update individual's memory
        for agent in self._team_blue + self._team_red:
            if agent.memory_mode == "fog":
                agent.update_memory(env=self)
        
        # Update team memory
        if self.TEAM_MEMORY == "fog":
            self._update_global_memory()

        info = {
                'blue_trajectory': self._blue_trajectory,
                'red_trajectory': self._red_trajectory,
                'static_map': self._static_map
            }

        return self.get_obs_blue, reward, isDone, info

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
        if (self.RENDER_INDIV_MEMORY == True and self.INDIV_MEMORY == "fog") or (self.RENDER_TEAM_MEMORY == True and self.TEAM_MEMORY == "fog"):
            SCREEN_W = 1200
            SCREEN_H = 600

            if self.viewer is None:
                from gym.envs.classic_control import rendering
                self.viewer = rendering.Viewer(SCREEN_W, SCREEN_H)
                self.viewer.set_bounds(0, SCREEN_W, 0, SCREEN_H)
    
            self.viewer.draw_polygon([(0, 0), (SCREEN_W, 0), (SCREEN_W, SCREEN_H), (0, SCREEN_H)], color=(0, 0, 0))

            self._env_render(self._static_map,
                            [7, 7], [SCREEN_H//2-10, SCREEN_H//2-10])
            self._env_render(self.get_obs_blue_render,
                            [7+1.49*SCREEN_H//3, 7], [SCREEN_H//2-10, SCREEN_H//2-10])
            self._env_render(self.get_obs_red_render,
                            [7+1.49*SCREEN_H//3, 7+1.49*SCREEN_H//3], [SCREEN_H//2-10, SCREEN_H//2-10])
            self._env_render(self.get_full_state,
                            [7, 7+1.49*SCREEN_H//3], [SCREEN_H//2-10, SCREEN_H//2-10])

            # ind blue agent memory rendering
            for num_blue, blue_agent in enumerate(self._team_blue):
                if num_blue < 2:
                    blue_agent.INDIV_MEMORY = self.INDIV_MEMORY
                    if blue_agent.INDIV_MEMORY == "fog" and self.RENDER_INDIV_MEMORY == True:
                        self._env_render(blue_agent.memory,
                                         [900+num_blue*SCREEN_H//4, 7], [SCREEN_H//4-10, SCREEN_H//4-10])
                else:
                    blue_agent.INDIV_MEMORY = self.INDIV_MEMORY
                    if blue_agent.INDIV_MEMORY == "fog" and self.RENDER_INDIV_MEMORY == True:
                        self._env_render(blue_agent.memory,
                                         [900+(num_blue-2)*SCREEN_H//4, 7+SCREEN_H//4], [SCREEN_H//4-10, SCREEN_H//4-10])

            # ind red agent memory rendering
            for num_red, red_agent in enumerate(self._team_red):
                if num_red < 2:
                    red_agent.INDIV_MEMORY = self.INDIV_MEMORY
                    if red_agent.INDIV_MEMORY == "fog" and self.RENDER_INDIV_MEMORY == True:
                        self._env_render(red_agent.memory,
                                         [900+num_red*SCREEN_H//4, 7+1.49*SCREEN_H//2], [SCREEN_H//4-10, SCREEN_H//4-10])
    
                else:
                    red_agent.INDIV_MEMORY = self.INDIV_MEMORY
                    if red_agent.INDIV_MEMORY == "fog" and self.RENDER_INDIV_MEMORY == True:
                        self._env_render(red_agent.memory,
                                         [900+(num_red-2)*SCREEN_H//4, 7+SCREEN_H//2], [SCREEN_H//4-10, SCREEN_H//4-10])

            if self.TEAM_MEMORY == "fog" and self.RENDER_TEAM_MEMORY == True:
                # blue team memory rendering
                self._env_render(self.blue_memory,
                                 [7+2.98*SCREEN_H//3, 7], [SCREEN_H//2-10, SCREEN_H//2-10])
                # red team memory rendering    
                self._env_render(self.red_memory,
                                 [7+2.98*SCREEN_H//3, 7+1.49*SCREEN_H//3], [SCREEN_H//2-10, SCREEN_H//2-10])
        else:
            SCREEN_W = 600
            SCREEN_H = 600
            
            if self.viewer is None:
                from gym.envs.classic_control import rendering
                self.viewer = rendering.Viewer(SCREEN_W, SCREEN_H)
                self.viewer.set_bounds(0, SCREEN_W, 0, SCREEN_H)
                
            self.viewer.draw_polygon([(0, 0), (SCREEN_W, 0), (SCREEN_W, SCREEN_H), (0, SCREEN_H)], color=(0, 0, 0))
            
            self._env_render(self._static_map,
                            [5, 10], [SCREEN_W//2-10, SCREEN_H//2-10])
            self._env_render(self.get_obs_blue_render,
                            [5+SCREEN_W//2, 10], [SCREEN_W//2-10, SCREEN_H//2-10])
            self._env_render(self.get_obs_red_render,
                            [5+SCREEN_W//2, 10+SCREEN_H//2], [SCREEN_W//2-10, SCREEN_H//2-10])
            self._env_render(self.get_full_state,
                            [5, 10+SCREEN_H//2], [SCREEN_W//2-10, SCREEN_H//2-10])
            self._agent_render(self.get_full_state,
                            [5, 10+SCREEN_H//2], [SCREEN_W//2-10, SCREEN_H//2-10])

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

    def _agent_render(self, env, rend_loc, rend_size):
        tile_w = rend_size[0] / len(env)
        tile_h = rend_size[1] / len(env[0])

        for entity in self._team_blue+self._team_red:
            if not entity.isAlive: continue
            x,y = entity.get_loc()
            locx, locy = rend_loc
            locx += x * tile_w
            locy += y * tile_h
            cur_color = COLOR_DICT[TEAM1_UGV] if entity.team == TEAM1_BACKGROUND else COLOR_DICT[TEAM2_UGV]
            cur_color = np.divide(cur_color, 255.0)
            self.viewer.draw_polygon([
                (locx, locy),
                (locx + tile_w, locy),
                (locx + tile_w, locy + tile_h),
                (locx, locy + tile_h)], color=cur_color)

            if entity.air:
                self.viewer.draw_polyline([
                    (locx, locy),
                    (locx + tile_w, locy + tile_h)],
                    color=(0,0,0), linewidth=2)
                self.viewer.draw_polyline([
                    (locx + tile_w, locy),
                    (locx, locy + tile_h)],
                    color=(0,0,0), linewidth=2)#col * tile_w, row * tile_h

            if entity.marker is not None:
                ratio = 0.6
                color = np.divide(entity.marker, 255.0)
                self.viewer.draw_polygon([
                    (locx + tile_w * ratio, locy + tile_h * ratio),
                    (locx + tile_w, locy + tile_h * ratio),
                    (locx + tile_w, locy + tile_h),
                    (locx + tile_w * ratio, locy + tile_h)], color=color)

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


# State space for capture the flag
class Board(spaces.Space):
    """A Board in R^3 used for CtF """
    def __init__(self, shape=None, dtype=np.uint8):
        assert dtype is not None, 'dtype must be explicitly provided. '
        self.dtype = np.dtype(dtype)

        if shape is None:
            self.shape = (20, 20, NUM_CHANNEL)
        else:
            assert shape[2] == NUM_CHANNEL
            self.shape = tuple(shape)
        super(Board, self).__init__(self.shape, self.dtype)

    def __repr__(self):
        return "Board" + str(self.shape)

    def sample(self):
        map_obj = [NUM_BLUE, NUM_UAV, NUM_RED, NUM_UAV, NUM_GRAY]
        state, _, _ = CreateMap.gen_map('map',
                self.shape[0], rand_zones=False, map_obj=map_obj)
        return state

