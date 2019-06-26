import unittest
import gym
import gym_cap
import numpy as np
import random
import time

import policy

ENV_NAME = 'cap-v0'

def repeat(times):
    " Wrapper for multiple test"
    def repeatwrapper(f):
        def callwrapper(*args):
            for i in range(times):
                f(*args)
        return callwrapper
    return repeatwrapper

class TestAgentGetObs(unittest.TestCase):

    @repeat(10)
    def testFrequency(self):
        " Communication frequency test"
        env = gym.make(ENV_NAME)
        env.COM_FREQUENCY = random.random()# Try random frequency
        env.reset()
        for entity in env.team_blue+env.team_red:
            entity.get_obs(env)
                
    @repeat(10)
    def testComAir(self):
        " Communication between ground and air test"
        env = gym.make(ENV_NAME)
        env.NUM_UAV = 2
        env.COM_AIR = True
        env.reset()
        for entity in env.team_blue+env.team_red:
            entity.get_obs(env)

    @repeat(10)
    def testComGround(self):
        " Communication between ground and ground test"
        env = gym.make(ENV_NAME, custom_board='board.txt')
        env.NUM_UAV = 2
        env.BLUE_PARTIAL = False
        env.COM_GROUND = True
        env.reset()
        #for entity in env.team_blue+env.team_red:
        entity = env.team_blue[0]

        print(entity.get_obs(env))



if __name__ == '__main__':
    unittest.main()
