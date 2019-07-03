import sys
import time
import gym
import gym_cap
import numpy as np
import random
import matplotlib.pyplot as plt
from tqdm import tqdm

# modules needed to generate policies
import policy
   
args_list = []
kwargs_list = ['episode', 'blue_policy', 'red_policy',
               'num_blue', 'num_red', 'num_uav',
               'map_size', 'time_step']
param = {}

i = 1
while i < len(sys.argv):
    key = sys.argv[i][2:]
    if key in kwargs_list:
        param[key] = sys.argv[i+1]
        i += 1
    elif key in args_list:
        param[key] = True
    else:
        raise AttributeError("Wrong argument name given")
    i += 1
    
# default cases
episode = param.get('episode', '5')
param['blue_policy'] = param.get('blue_policy', 'Random')
param['red_policy'] = param.get('red_policy', 'Random')
blue_policy = getattr(policy, param['blue_policy'])()
red_policy = getattr(policy, param['red_policy'])()
map_size = param.get('map_size', '20')
time_step = param.get('time_step', '150')

# TODO: Make several other test board for evaluation
fair_maps = ['test_maps/board{}.txt'.format(i) for i in range(1,4)] 

if __name__ == '__main__':
    
    # initialize the environment
    env = gym.make(
            "cap-v0",
            map_size = int(map_size),
            policy_blue = blue_policy,
            policy_red = red_policy,
            custom_board=random.choice(fair_maps)
        )
    # TODO: add configuration file or add path to the program argument
    env.BLUE_PARTIAL = False
    env.RED_PARTIAL = False
    env.STOCH_ATTACK = True
    env.STOCH_ATTACK_BIAS = 3
    
    game_finish = False
    steps = 0
    team = ["NEITHER", "BLUE", "RED"]
    red_score = [0]
    blue_score = []
    statw = []
    statf = []
    statd = []
    times = []
    asteps = []
    
    # reset the environment and select the policies for each of the team
    observation = env.reset(
        )
    
    start_time = time.time()
    for iterate in tqdm(range(int(episode))):
        iter_time = time.time()
        while not game_finish:
    
            # feedback from environment
            observation, reward, game_finish, info = env.step()
    
            steps += 1
            if steps == int(time_step):
                # break after 150 or 'time_step' steps
                break 

        episode_time = time.time() - iter_time
        
        result = team[1] if (env.blue_win == True and env.red_win == False) else team[2] if (env.red_win == True and env.blue_win == False) else team[0]
        flag = team[1] if (env.blue_flag == True and env.red_die == False) else team[2] if (env.red_flag == True and env.blue_die == False) else team[0]
        die = team[1] if (env.blue_die == True and env.red_die == False) else team[2] if (env.red_die == True and env.blue_die == False) else team[0]
        statw.append(result)
        statf.append(flag)
        statd.append(die)
        times.append(episode_time)
        asteps.append(steps)
        
        env.reset(custom_board=random.choice(fair_maps))
        game_finish = False
        
        blue_score.append(reward)
    
    total_time = time.time() - start_time
    print("--------------------------------------- Statistics ---------------------------------------")
    win = dict(zip(*np.unique(statw, return_counts=True)))
    winf = dict(zip(*np.unique(statf, return_counts=True)))
    wind = dict(zip(*np.unique(statd, return_counts=True)))
    print("win # overall in {} episodes: {}\nwin # in capturing flag\t   : {}\nwin # in killing other team: {}\ntime per episode: {} s\ntotal time: {} s\nmean steps: {}"
          .format(episode, win, winf, wind, np.mean(times), total_time, np.mean(asteps)))
    blue_plot = plt.hist(blue_score)
    plt.xlabel("Blue Team Mean Score")
    plt.ylabel("Blue Team")
    plt.show(blue_plot)
    
    # closing CtF environment           
    env.close()
    del gym.envs.registry.env_specs['cap-v0']
