import os, csv
import sys

import errno
import pudb

from collections import defaultdict
from copy import deepcopy

import torch
import torch as th
import torch.optim as optim
import torch.nn.functional as F

from torch.autograd import Variable
from tensorboardX import SummaryWriter
from torchviz import make_dot

import numpy as np
from scipy.constants import golden

import matplotlib.pyplot as plt
import seaborn as sns

import skimage
from skimage import data, io

import gym
from gym import wrappers
import azad.local_gym
from azad.local_gym.wythoff import create_moves
from azad.local_gym.wythoff import create_all_possible_moves
from azad.local_gym.wythoff import locate_moves
from azad.local_gym.wythoff import create_cold_board
from azad.local_gym.wythoff import create_board
from azad.local_gym.wythoff import cold_move_available
from azad.local_gym.wythoff import locate_closest_cold_move
from azad.local_gym.wythoff import locate_cold_moves

from azad.models import Table
from azad.models import DeepTable3
from azad.models import HotCold2
from azad.models import HotCold3
from azad.models import ReplayMemory
from azad.policy import epsilon_greedy
from azad.policy import softmax


class WythoffOptimalStrategist(object):
    """Mimic an optimal Wythoffs player, while behaving like a pytorch model."""

    def __init__(self, m, n, hot_value=-1, cold_value=1):
        self.m = int(m)
        self.n = int(m)
        self.hot_value = float(hot_value)
        self.cold_value = float(cold_value)

        self.board = create_cold_board(
            self.m, self.n, cold_value=cold_value, default=hot_value)

    def forward(self, x):
        try:
            x = tuple(x.detach().numpy().flatten())
        except AttributeError:
            pass

        i, j = x

        return self.board[int(i), int(j)]

    def __call__(self, x):
        return self.forward(x)


def wythoff_stumbler_strategist(num_episodes=10,
                                num_stumbles=1000,
                                stumbler_game='Wythoff10x10',
                                learning_rate_stumbler=0.1,
                                epsilon=0.5,
                                anneal=True,
                                gamma=1.0,
                                num_strategies=1000,
                                strategist_game='Wythoff50x50',
                                learning_rate_strategist=0.01,
                                num_hidden1=100,
                                num_hidden2=25,
                                cold_threshold=0.0,
                                hot_threshold=0.5,
                                hot_value=1,
                                cold_value=-1,
                                reflect_cold=True,
                                optimal_strategist=False,
                                num_eval=1,
                                learning_rate_influence=0.01,
                                new_rules=False,
                                tensorboard=None,
                                update_every=5,
                                seed=None,
                                save=None,
                                load_model=None,
                                save_model=False,
                                stumbler_monitor=None,
                                strategist_monitor=None,
                                monitor=None,
                                return_none=False,
                                debug=False):
    """Learn Wythoff's with a stumbler-strategist network"""

    # -----------------------------------------------------------------------
    # Init games
    m, n, _, _ = peek(create_env(strategist_game, monitor=False))
    o, p, _, _ = peek(create_env(stumbler_game, monitor=False))

    if tensorboard:
        try:
            os.makedirs(tensorboard)
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise

        writer = SummaryWriter(log_dir=tensorboard)

    if monitor:
        monitored = create_monitored(monitor)

    # Force some casts, mostly to make CL invocation seamless
    num_episodes = int(num_episodes)
    num_strategies = int(num_strategies)
    num_stumbles = int(num_stumbles)
    num_eval = int(num_eval)
    num_hidden1 = int(num_hidden1)
    num_hidden2 = int(num_hidden2)

    # -----------------------------------------------------------------------
    # Init agents
    player = None
    opponent = None
    strategist = None
    bias_board = None

    # Override w/ data from disk?
    if load_model is not None:
        if debug:
            print(">>> Loading model from {}".format(load_model))

        player, opponent = load_stumbler(player, opponent, load_model)
        strategist = init_strategist(num_hidden1, num_hidden2)
        strategist = load_strategist(strategist, load_model)

    # If the rules have changed (wythoff -> nim,euclid) only the stumblers
    # action spaces don't match, and should be reset.
    if new_rules:
        player, opponent = None, None

    # Optimal overrides all others
    if optimal_strategist:
        strategist = WythoffOptimalStrategist(
            m, n, hot_value=hot_value, cold_value=cold_value)
        score_b = 0.0

    # -----------------------------------------------------------------------
    influence = 0.0
    score_a = 0.0
    score_b = 0.0
    total_reward_a = 0.0
    for episode in range(num_episodes):
        # Stumbler
        save_a = None
        if save is not None:
            save_a = save + "_episode{}_stumbler".format(episode)

        (player, opponent), (score_a, total_reward_a) = wythoff_stumbler(
            num_episodes=num_stumbles,
            game=stumbler_game,
            epsilon=epsilon,
            anneal=anneal,
            gamma=gamma,
            learning_rate=learning_rate_stumbler,
            model=player,
            opponent=opponent,
            bias_board=bias_board,
            influence=influence,
            score=score_a,
            total_reward=total_reward_a,
            tensorboard=tensorboard,
            update_every=update_every,
            initial=episode * num_stumbles,
            debug=debug,
            save=save_a,
            save_model=False,
            monitor=stumbler_monitor,
            return_none=False,
            seed=seed)

        # Strategist
        if not optimal_strategist:
            save_b = None
            if save is not None:
                save_b = save + "_episode{}_strategist".format(episode)

            strategist, score_b = wythoff_strategist(
                player,
                stumbler_game,
                num_episodes=num_strategies,
                game=strategist_game,
                model=strategist,
                num_hidden1=num_hidden1,
                num_hidden2=num_hidden2,
                score=score_b,
                cold_threshold=cold_threshold,
                hot_threshold=hot_threshold,
                learning_rate=learning_rate_strategist,
                tensorboard=tensorboard,
                update_every=update_every,
                hot_value=hot_value,
                cold_value=cold_value,
                reflect_cold=reflect_cold,
                initial=episode * num_strategies,
                debug=debug,
                save=save_b,
                monitor=strategist_monitor,
                save_model=False,
                return_none=False,
                seed=seed)

        # --------------------------------------------------------------------
        # Use the trained strategist to generate a bias_board,
        bias_board = create_bias_board(m, n, strategist)

        # Est performance. Count strategist wins.
        wins, eval_score_a, eval_score_b = evaluate_wythoff(
            player,
            strategist,
            stumbler_game,
            strategist_game,
            num_episodes=num_eval,
            debug=debug)

        # Update the influence and then the bias_board
        win = wins / num_eval
        if win > 0.5:
            influence += learning_rate_influence
        else:
            influence -= learning_rate_influence
        influence = np.clip(influence, 0, 1)

        # --------------------------------------------------------------------
        if tensorboard:
            writer.add_scalar('stategist_influence', influence, episode)
            writer.add_scalar('stategist_eval_score', eval_score_b, episode)
            writer.add_scalar('stumbler_eval_score', eval_score_a, episode)

        if monitor:
            all_variables = locals()
            for k in monitor:
                monitored[k].append(float(all_variables[k]))

    # --------------------------------------------------------------------
    # Clean up
    if tensorboard:
        writer.close()

    # Save?
    if save_model:
        state = {
            'strategist_model_dict': strategist.state_dict(),
            "num_hidden1": num_hidden1,
            "num_hidden2": num_hidden2,
            'stumbler_player_dict': player,
            'stumbler_opponent_dict': opponent
        }
        torch.save(state, save + ".pytorch")

    if monitor:
        save_monitored(save, monitored)

    result = (player, strategist), (score_a, influence)
    if return_none:
        result = None

    return result


def load_stumbler(model, opponent, load_model):
    state = th.load(load_model)
    model = state["stumbler_player_dict"]
    opponent = state["stumbler_opponent_dict"]

    return model, opponent


def wythoff_stumbler(num_episodes=10,
                     epsilon=0.1,
                     gamma=0.8,
                     learning_rate=0.1,
                     game='Wythoff10x10',
                     model=None,
                     opponent=None,
                     anneal=False,
                     bias_board=None,
                     influence=0.0,
                     score=0.0,
                     total_reward=0.0,
                     tensorboard=None,
                     update_every=5,
                     initial=0,
                     self_play=False,
                     save=False,
                     load_model=None,
                     save_model=False,
                     monitor=None,
                     return_none=False,
                     debug=False,
                     seed=None):
    """Learn to play Wythoff's w/ e-greedy random exploration.
    
    Note: Learning is based on a player-opponent joint action formalism 
    and tabular Q-learning.
    """

    # ------------------------------------------------------------------------
    # Init env
    if tensorboard is not None:
        try:
            os.makedirs(tensorboard)
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise
        writer = SummaryWriter(log_dir=tensorboard)

    # Create env
    if tensorboard is not None:
        env = create_env(game, monitor=True)
    else:
        env = create_env(game, monitor=False)
    env.seed(seed)
    np.random.seed(seed)

    if monitor is not None:
        monitored = create_monitored(monitor)

    # ------------------------------------------------------------------------
    # Init Agents
    default_Q = 0.0
    m, n, board, available = peek(env)
    if model is None:
        model = {}
    if opponent is None:
        opponent = {}

    # Override from file?
    if load_model is not None:
        if debug:
            print(">>> Loadiing model/opponent from {}".format(load_model))

        model, opponent = load_stumbler(model, opponent, load_model)

    # ------------------------------------------------------------------------
    for episode in range(initial, initial + num_episodes):
        # Re-init
        steps = 1

        x, y, board, available = env.reset()
        board = tuple(flatten_board(board))
        if debug:
            print("---------------------------------------")
            print(">>> NEW GAME ({}).".format(episode))
            print(">>> Initial position ({}, {})".format(x, y))
            print(">>> Initial moves {}".format(available))
            print("---------------------------------------")

        t_state = [
            board,
        ]
        t_available = [available]
        t_move = []
        t_move_i = []
        t_reward = []

        # -------------------------------------------------------------------
        # Anneal epsilon?
        if anneal:
            epsilon_e = epsilon * (1.0 / np.log((episode + np.e)))
        else:
            epsilon_e = episode

        # -------------------------------------------------------------------
        # Play!
        done = False
        player_win = False
        while not done:
            # PLAYER CHOOSES A MOVE
            try:
                Qs_episode = add_bias_board(model[board], available,
                                            bias_board, influence)
                move_i = epsilon_greedy(
                    Qs_episode, epsilon=epsilon_e, mode='numpy')
            except KeyError:
                model[board] = np.ones(len(available)) * default_Q
                move_i = np.random.randint(0, len(available))

            move = available[move_i]

            # Analyze it...
            best = 0.0
            if cold_move_available(x, y, available):
                if move in locate_cold_moves(x, y, available):
                    best = 1.0
                score += (best - score) / (episode + 1)

            # PLAY THE MOVE
            (x, y, board, available), reward, done, _ = env.step(move)
            board = tuple(flatten_board(board))
            steps += 1

            # Log....
            if debug:
                print(">>> PLAYER move {}".format(move))

            t_state.append(board)
            t_move.append(move)
            t_available.append(available)
            t_move_i.append(move_i)
            t_reward.append(reward)

            if done:
                player_win = True
                t_state.append(board)
                t_move.append(move)
                t_available.append(available)
                t_move_i.append(move_i)
                t_reward.append(reward)

            # ----------------------------------------------------------------
            if not done:
                # OPPONENT CHOOSES A MOVE
                try:
                    Qs_episode = add_bias_board(opponent[board], available,
                                                bias_board, influence)
                    move_i = epsilon_greedy(
                        Qs_episode, epsilon=epsilon_e, mode='numpy')
                except KeyError:
                    opponent[board] = np.ones(len(available)) * default_Q
                    move_i = np.random.randint(0, len(available))

                move = available[move_i]

                # PLAY THE MOVE
                (x, y, board, available), reward, done, _ = env.step(move)
                board = tuple(flatten_board(board))
                steps += 1

                # Log....
                if debug:
                    print(">>> OPPONENT move {}".format(move))

                t_state.append(board)
                t_move.append(move)
                t_available.append(available)
                t_move_i.append(move_i)
                t_reward.append(reward)

                if done:
                    t_state.append(board)
                    t_move.append(move)
                    t_available.append(available)
                    t_move_i.append(move_i)
                    t_reward.append(reward)

        # ----------------------------------------------------------------
        # Learn by unrolling the last game...

        # PLAYER (model)
        s_idx = np.arange(0, steps - 1, 2)
        for i in s_idx:
            # States and actions
            s = t_state[i]
            next_s = t_state[i + 2]
            m_i = t_move_i[i]

            # Value and reward
            Q = model[s][m_i]

            try:
                max_Q = model[next_s].max()
            except KeyError:
                model[next_s] = np.ones(len(t_available[i])) * default_Q
                max_Q = model[next_s].max()

            if player_win:
                r = t_reward[i]
            else:
                r = -1 * t_reward[i + 1]

            # Update running reward total for player
            total_reward += r

            # Loss and learn
            next_Q = r + (gamma * max_Q)
            loss = next_Q - Q
            model[s][m_i] = Q + (learning_rate * loss)

        # OPPONENT
        s_idx = np.arange(1, steps - 1, 2)
        for i in s_idx:
            # States and actions
            s = t_state[i]
            next_s = t_state[i + 2]
            m_i = t_move_i[i]

            # Value and reward
            Q = opponent[s][m_i]

            try:
                max_Q = opponent[next_s].max()
            except KeyError:
                opponent[next_s] = np.ones(len(t_available[i])) * default_Q
                max_Q = opponent[next_s].max()

            if not player_win:
                r = t_reward[i]
            else:
                r = -1 * t_reward[i + 1]

            # Loss and learn
            next_Q = r + (gamma * max_Q)
            loss = next_Q - Q
            opponent[s][m_i] = Q + (learning_rate * loss)

        # ----------------------------------------------------------------
        # Update the log
        if debug:
            print(">>> Reward {}; Loss(Q {}, next_Q {}) -> {}".format(
                r, Q, next_Q, loss))

            if done and (r > 0):
                print("*** WIN ***")
            if done and (r < 0):
                print("*** OPPONENT WIN ***")

        if tensorboard and (int(episode) % update_every) == 0:
            writer.add_scalar('reward', r, episode)
            writer.add_scalar('Q', Q, episode)
            writer.add_scalar('epsilon_e', epsilon_e, episode)
            writer.add_scalar('stumber_error', loss, episode)
            writer.add_scalar('stumber_steps', steps, episode)
            writer.add_scalar('stumbler_score', score, episode)

            # Cold ref:
            cold = create_cold_board(m, n)
            plot_wythoff_board(
                cold, vmin=0, vmax=1, path=tensorboard, name='cold_board.png')
            writer.add_image(
                'cold_positions',
                skimage.io.imread(os.path.join(tensorboard, 'cold_board.png')))

            # Agent max(Q) boards
            values = expected_value(m, n, model)
            plot_wythoff_board(
                values, path=tensorboard, name='player_max_values.png')
            writer.add_image(
                'player',
                skimage.io.imread(
                    os.path.join(tensorboard, 'player_max_values.png')))

            values = expected_value(m, n, opponent)
            plot_wythoff_board(
                values, path=tensorboard, name='opponent_max_values.png')
            writer.add_image(
                'opponent',
                skimage.io.imread(
                    os.path.join(tensorboard, 'opponent_max_values.png')))

        if monitor and (int(episode) % update_every) == 0:
            all_variables = locals()
            for k in monitor:
                monitored[k].append(float(all_variables[k]))

    # --------------------------------------------------------------------
    if save_model:
        state = {
            'stumbler_player_dict': model,
            'stumbler_opponent_dict': opponent
        }
        torch.save(state, save + ".pytorch")

    if monitor:
        save_monitored(save, monitored)

    if tensorboard:
        writer.close()

    result = (model, opponent), (score, total_reward)
    if return_none:
        result = None

    return result


def load_strategist(model, load_model):
    """Override model with parameters from file"""
    state = th.load(load_model)
    model.load_state_dict(state["strategist_model_dict"])

    return model


def init_strategist(num_hidden1, num_hidden2):
    """Create a Wythoff's game strategist"""

    num_hidden1 = int(num_hidden1)
    num_hidden2 = int(num_hidden2)
    if num_hidden2 > 0:
        model = HotCold3(2, num_hidden1=num_hidden1, num_hidden2=num_hidden2)
    else:
        model = HotCold2(2, num_hidden1=num_hidden1)

    return model


def wythoff_strategist(stumbler_model,
                       stumbler_game,
                       num_episodes=1000,
                       cold_threshold=0.0,
                       hot_threshold=0.5,
                       hot_value=1,
                       cold_value=-1,
                       learning_rate=0.01,
                       game='Wythoff50x50',
                       model=None,
                       num_hidden1=100,
                       num_hidden2=25,
                       initial=0,
                       score=0.0,
                       tensorboard=None,
                       stumbler_mode='numpy',
                       balance_cold=False,
                       reflect_cold=True,
                       update_every=50,
                       save=None,
                       load_model=None,
                       save_model=False,
                       monitor=None,
                       return_none=False,
                       debug=False,
                       seed=None):
    """Learn a generalizable strategy for Wythoffs game"""

    # ------------------------------------------------------------------------
    # Setup
    if tensorboard is not None:
        try:
            os.makedirs(tensorboard)
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise

        writer = SummaryWriter(log_dir=tensorboard)

    # Create env and find all moves in it

    # Create env
    if tensorboard is not None:
        env = create_env(game, monitor=True)
    else:
        env = create_env(game, monitor=False)
    env.seed(seed)
    np.random.seed(seed)
    o, p, _, _ = peek(create_env(stumbler_game, monitor=False))

    m, n, board, _ = peek(env)
    all_possible_moves = create_all_possible_moves(m, n)

    # Watch vars?
    if monitor:
        monitored = create_monitored(monitor)

    # Init strategist
    if model is None:
        model = init_strategist(num_hidden1, num_hidden2)

    # Add old weights from file?
    if load_model is not None:
        if debug:
            print(">>> Loading model from {}".format(load_model))
        model = load_strategist(model, load_model)

    # Init SGD.
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)

    # ------------------------------------------------------------------------
    # Extract strategic data from the stumbler
    strategic_default_value = 0.0
    if hot_threshold is None:
        strategic_value = estimate_cold(
            m,
            n,
            stumbler_model,
            threshold=cold_threshold,
            value=cold_value,
            reflect=reflect_cold,
            default_value=strategic_default_value)
    elif cold_threshold is None:
        strategic_value = estimate_hot(
            m,
            n,
            stumbler_model,
            threshold=hot_threshold,
            value=hot_value,
            default_value=strategic_default_value)
    else:
        strategic_value = estimate_hot_cold(
            o,
            p,
            stumbler_model,
            hot_threshold=hot_threshold,
            cold_threshold=cold_threshold,
            hot_value=hot_value,
            cold_value=cold_value,
            reflect_cold=reflect_cold,
            default_value=strategic_default_value)

    # Convert format.
    s_data = convert_ijv(strategic_value)
    if balance_cold:
        s_data = balance_ijv(s_data, cold_value)

    # Sanity?
    if s_data is None:
        return model, None

    # Define a memory to sample.
    memory = ReplayMemory(len(s_data))
    batch_size = len(s_data)
    for d in s_data:
        memory.push(*d)

    # ------------------------------------------------------------------------
    # Sample the memory to teach the strategist
    bias_board = None
    for episode in range(initial, initial + num_episodes):
        loss = 0.0

        if debug:
            print("---------------------------------------")
            print(">>> STRATEGIST ({}).".format(episode))

        coords = []
        values = []
        for c, v in memory.sample(batch_size):
            coords.append(c)
            values.append(v)
        coords = torch.tensor(
            np.vstack(coords), requires_grad=True, dtype=torch.float)
        values = torch.tensor(values, requires_grad=False, dtype=torch.float)

        # Making some preditions, ...
        predicted_values = model(coords).squeeze()

        # and learn from them
        loss = F.mse_loss(predicted_values, values)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # --------------------------------------------------------------------
        if debug:
            print(">>> Coords: {}".format(coords))
            print(">>> Values: {}".format(values))
            print(">>> Predicted values: {}".format(values))
            print(">>> Loss {}".format(loss))

        if tensorboard and (int(episode) % update_every) == 0:
            # Timecourse
            writer.add_scalar('stategist_error', loss, episode)

            bias_board = create_bias_board(m, n, model)
            plot_wythoff_board(
                bias_board,
                vmin=-1.5,
                vmax=1.5,
                path=tensorboard,
                height=10,
                width=15,
                name='bias_board.png')
            writer.add_image(
                'strategist',
                skimage.io.imread(os.path.join(tensorboard, 'bias_board.png')))

        if monitor and (int(episode) % update_every) == 0:
            # Score the model:
            with th.no_grad():
                pred = create_bias_board(m, n, model, default=0.0).numpy()
                cold = create_cold_board(m, n, default=hot_value)
                mae = np.median(np.abs(pred - cold))

            all_variables = locals()
            for k in monitor:
                monitored[k].append(float(all_variables[k]))

    # Final score for the model:
    with th.no_grad():
        pred = create_bias_board(m, n, model, default=0.0).numpy()
        cold = create_cold_board(m, n, default=hot_value)
        mae = np.median(np.abs(pred - cold))

    # Save?
    if save_model:
        state = {
            'strategist_model_dict': model.state_dict(),
            "num_hidden1": num_hidden1,
            "num_hidden2": num_hidden2
        }
        th.save(state, save + ".pytorch")

    if monitor:
        save_monitored(save, monitored)

    # Suppress return for parallel runs?
    result = (model), (mae)
    if return_none:
        result = None

    return result


def wythoff_oracular_strategy(path,
                              num_episodes=1000,
                              learning_rate=0.025,
                              num_hidden1=100,
                              num_hidden2=25,
                              stumbler_game='Wythoff10x10',
                              strategist_game='Wythoff50x50',
                              tensorboard=None,
                              update_every=50,
                              save=None,
                              return_none=False,
                              debug=False,
                              seed=None):
    """Train a strategist layer on perfact data."""

    # ------------------------------------------------------------------------
    # Setup
    if tensorboard is not None:
        try:
            os.makedirs(tensorboard)
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise

        writer = SummaryWriter(log_dir=tensorboard)

    # Create env
    if tensorboard is not None:
        env = create_env(game, monitor=True)
    else:
        env = create_env(game, monitor=False)

    # Boards, etc
    m, n, board, _ = peek(create_env(strategist_game))
    o, p, _, _ = peek(create_env(stumbler_game))
    if debug:
        print(">>> TRANING AN OPTIMAL STRATEGIST.")
        print(">>> Train board {}".format(o, p))
        print(">>> Test board {}".format(m, n))

    # Seeding...
    env.seed(seed)
    np.random.seed(seed)

    # Train params
    strategic_default_value = 0.0
    batch_size = 64

    # ------------------------------------------------------------------------
    # Build a Strategist, its memory, and its optimizer

    # Create a model, of the right size.
    # model = HotCold2(2, num_hidden1=num_hidden1)
    model = HotCold3(2, num_hidden1=num_hidden1, num_hidden2=num_hidden2)

    optimizer = optim.Adam(model.parameters(), lr=learning_rate)
    memory = ReplayMemory(10000)

    # Run learning episodes. The 'stumbler' is just the opt
    # cold board
    for episode in range(num_episodes):
        # The cold spots are '1' everythig else is '0'
        strategic_value = create_cold_board(o, p)

        # ...Into tuples
        s_data = convert_ijv(strategic_value)
        s_data = balance_ijv(s_data, strategic_default_value)

        for d in s_data:
            memory.push(*d)

        loss = 0.0
        if len(memory) > batch_size:
            # Sample data....
            coords = []
            values = []
            samples = memory.sample(batch_size)

            for c, v in samples:
                coords.append(c)
                values.append(v)

            coords = torch.tensor(
                np.vstack(coords), requires_grad=True, dtype=torch.float)
            values = torch.tensor(
                values, requires_grad=False, dtype=torch.float)

            # Making some preditions,
            predicted_values = model(coords).squeeze()

            # and find their loss.
            loss = F.mse_loss(predicted_values, values)

            # Walk down the hill of righteousness!
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if debug:
                print(">>> Coords: {}".format(coords))
                print(">>> Values: {}".format(values))
                print(">>> Predicted values: {}".format(values))
                print(">>> Loss {}".format(loss))

        # Use the trained strategist to generate a bias_board,
        bias_board = create_bias_board(m, n, model)

        if tensorboard and (int(episode) % update_every) == 0:
            writer.add_scalar(os.path.join(path, 'error'), loss, episode)

            plot_wythoff_board(
                strategic_value,
                vmin=0,
                vmax=1,
                path=path,
                name='strategy_board_{}.png'.format(episode))
            writer.add_image(
                'Training board',
                skimage.io.imread(
                    os.path.join(path,
                                 'strategy_board_{}.png'.format(episode))))

            plot_wythoff_board(
                bias_board,
                vmin=0,
                vmax=1,
                path=path,
                name='bias_board_{}.png'.format(episode))
            writer.add_image(
                'Testing board',
                skimage.io.imread(
                    os.path.join(path, 'bias_board_{}.png'.format(episode))))

    # The end
    if tensorboard:
        writer.close()

    # Suppress return for parallel runs?
    result = (model), (loss)
    if return_none:
        result = None

    return result


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HELPER FNs
def create_monitored(monitor):
    """Create a dictionary for monitoring"""
    # Build a structure for monitored variables
    monitored = {}
    for k in monitor:
        monitored[k] = list()

    return monitored


def save_monitored(save, monitored):
    """Save monitored data to a .csv"""

    with open(save + '_monitor.csv', 'w') as csv_file:
        keys = sorted(monitored.keys())
        values = [monitored[k] for k in keys]

        w = csv.writer(csv_file)
        w.writerow(keys)
        for row in zip(*values):
            w.writerow(row)


def add_bias_board(Qs, available, bias_board, influence):
    """Add bias to Qs."""

    assert len(Qs) == len(available), "Qs/available mismatch."

    if bias_board is None:
        return Qs
    if np.isclose(influence, 0.0):
        return Qs

    for i, (x, y) in enumerate(available):
        Qs[i] = Qs[i] + (influence * bias_board[x, y])

    return Qs


def convert_ijv(data):
    """Convert a (m,n) matrix into a list of (i, j, value)"""

    m, n = data.shape
    converted = []
    for i in range(m):
        for j in range(n):
            converted.append([(i, j), data[i, j]])

    return converted


def balance_ijv(ijv_data, target_value):
    """Balance counts of target versus other values"""

    # Separate data based on target_value
    other = []
    null = []
    for c, v in ijv_data:
        if np.isclose(v, target_value):
            null.append([c, v])
        else:
            other.append([c, v])

    # Sizes
    N_null = len(null)
    N_other = len(other)

    # Sanity?
    if N_null == 0:
        return None
    if N_other == 0:
        return None

    np.random.shuffle(null)
    np.random.shuffle(other)

    # Finally, balance one or the other. Who is bigger?
    if N_null > N_other:
        null = null[:N_other]
    elif N_other > N_null:
        other = other[:N_null]
    else:
        # They already balanced. Return I
        return ijv_data

    return null + other


def expected_value(m, n, model, default_value=0.0):
    """Estimate the max value of each board position"""

    values = np.zeros((m, n))
    all_possible_moves = create_all_possible_moves(m, n)
    for i in range(m):
        for j in range(n):
            board = tuple(flatten_board(create_board(i, j, m, n)))
            try:
                v = model[board].max()
                values[i, j] = v
            except KeyError:
                values[i, j] = default_value

    return values


def estimate_cold(m,
                  n,
                  model,
                  threshold=0.0,
                  value=-1,
                  default_value=0.0,
                  reflect=True):
    """Estimate cold positions, enforcing symmetry on the diagonal"""
    values = expected_value(m, n, model, default_value=default_value)
    cold = np.zeros_like(values)

    # Cold
    mask = values < threshold
    cold[mask] = value

    if reflect:
        cold[mask.transpose()] = value

    return cold


def estimate_hot(m, n, model, threshold=0.5, value=1, default_value=0.0):
    """Estimate hot positions"""
    values = expected_value(m, n, model, default_value=default_value)
    hot = np.zeros_like(values)

    mask = values > threshold
    hot[mask] = value

    return hot


def estimate_hot_cold(m,
                      n,
                      model,
                      hot_threshold=0.5,
                      cold_threshold=0.25,
                      cold_value=-1,
                      hot_value=1,
                      reflect_cold=True,
                      default_value=0.0):
    """Estimate hot and cold positions"""
    hot = estimate_hot(
        m,
        n,
        model,
        hot_threshold,
        value=hot_value,
        default_value=default_value)
    cold = estimate_cold(
        m,
        n,
        model,
        cold_threshold,
        value=cold_value,
        reflect=reflect_cold,
        default_value=default_value)

    return hot + cold


def create_bias_board(m, n, strategist_model, default=0.0):
    """"Sample all positions' value in a strategist model"""
    bias_board = torch.ones((m, n), dtype=torch.float) * default

    with torch.no_grad():
        for i in range(m):
            for j in range(n):
                coords = torch.tensor([i, j], dtype=torch.float)
                bias_board[i, j] = strategist_model(coords)

    return bias_board


def peek(env):
    """Peak at the env's board"""
    x, y, board, moves = env.reset()
    env.close()
    m, n = board.shape

    return m, n, board, moves


def flatten_board(board):
    m, n = board.shape
    return board.reshape(m * n)


def create_env(wythoff_name, monitor=True):
    env = gym.make('{}-v0'.format(wythoff_name))
    if monitor:
        env = wrappers.Monitor(
            env, './tmp/{}-v0-1'.format(wythoff_name), force=True)

    return env


def plot_wythoff_board(board,
                       vmin=-1.5,
                       vmax=1.5,
                       plot=False,
                       path=None,
                       height=2,
                       width=3,
                       name='wythoff_board.png'):
    """Plot the board"""

    fig, ax = plt.subplots(figsize=(width, height))  # Sample figsize in inches
    ax = sns.heatmap(board, linewidths=3, vmin=vmin, vmax=vmax, ax=ax)

    # Save an image?
    if path is not None:
        plt.savefig(os.path.join(path, name))

    if plot:
        # plt.show()
        plt.pause(0.01)

    plt.close('all')


def load_for_eval(stumbler_strategist):
    """Load a saved model."""
    state = th.load(stumbler_strategist)
    try:
        num_hidden1 = state["num_hidden1"]
        num_hidden2 = state["num_hidden2"]
        strategist = init_strategist(num_hidden1, num_hidden2)
        strategist = load_strategist(strategist, stumbler_strategist)
    except KeyError:
        print(
            ">>> Couldn't load strategist from {}".format(stumbler_strategist))
        strategist = None

    player, opponent = None, None
    player, opponent = load_stumbler(player, opponent, stumbler_strategist)

    return player, opponent, strategist


def evaluate_wythoff(stumbler=None,
                     strategist=None,
                     stumbler_game='Wythoff10x10',
                     strategist_game='Wythoff50x50',
                     random_stumbler=False,
                     load_model=None,
                     save=None,
                     return_none=False,
                     num_episodes=100,
                     debug=False):
    """Compare stumblers to strategists.
    
    Returns 
    -------
    wins : float
        the fraction of games won by the strategist.
    """
    # ------------------------------------------------------------------------
    if load_model is not None:
        stumbler, _, strategist = load_for_eval(load_model)

    # Init boards, etc
    # Stratgist
    env = create_env(strategist_game, monitor=False)
    m, n, board, _ = peek(env)
    if strategist is not None:
        hot_cold_table = create_bias_board(m, n, strategist)
    else:
        hot_cold_table = np.zeros_like(board)

    # Stumbler
    o, p, _, _ = peek(create_env(stumbler_game, monitor=False))

    # ------------------------------------------------------------------------
    # A stumbler and a strategist take turns playing a (m,n) game of wythoffs
    wins = 0.0
    strategist_score = 0.0
    stumbler_score = 0.0
    for episode in range(num_episodes):
        # Re-init
        steps = 0

        # Start the game, and process the result
        x, y, board, available = env.reset()
        board = tuple(flatten_board(board))

        if debug:
            print("---------------------------------------")
            print(">>> NEW MODEL EVALUATION ({}).".format(episode))
            print(">>> Initial position ({}, {})".format(x, y))

        done = False
        while not done:
            # ----------------------------------------------------------------
            # STUMBLER
            if (x < o) and (y < p):
                s_board = tuple(flatten_board(create_board(x, y, o, p)))
                s_available = create_moves(x, y)
                try:
                    values = stumbler[s_board]
                    move_i = epsilon_greedy(values, epsilon=0.0, mode='numpy')
                    move = s_available[move_i]
                except KeyError:
                    move_i = np.random.randint(0, len(s_available))
                    move = s_available[move_i]
            else:
                s_available = available
                move_i = np.random.randint(0, len(s_available))
                move = s_available[move_i]

            # ----------------------------------------------------------------
            # RANDOM PLAYER
            if random_stumbler:
                move_i = np.random.randint(0, len(available))
                move = available[move_i]

            # Analyze the choice
            best = 0.0
            if cold_move_available(x, y, s_available):
                if move in locate_cold_moves(x, y, s_available):
                    best = 1.0
                stumbler_score += (best - stumbler_score) / (episode + 1)

            # Move
            (x, y, board, available), reward, done, _ = env.step(move)
            board = tuple(flatten_board(board))
            if debug:
                print(">>> STUMBLER move {}".format(move))

            if done:
                break

            # ----------------------------------------------------------------
            # STRATEGIST
            # Choose.
            hot_cold_move_values = [hot_cold_table[i, j] for i, j in available]
            move_i = epsilon_greedy(
                np.asarray(hot_cold_move_values), epsilon=0.0, mode='numpy')
            move = available[move_i]

            if debug:
                print(">>> STRATEGIST move {}".format(move))

            # Analyze the choice
            best = 0.0
            if cold_move_available(x, y, available):
                if move in locate_cold_moves(x, y, available):
                    best = 1.0
                strategist_score += (best - strategist_score) / (episode + 1)

            # Make a move
            (x, y, board, available), reward, done, _ = env.step(move)
            board = tuple(flatten_board(board))
            if done:
                wins += 1.0
                break

        if debug:
            print("Wins {}, Scores ({}, {})".format(wins, stumbler_score,
                                                    strategist_score))

    if save is not None:
        np.savetxt(
            save,
            np.asarray([wins, stumbler_score, strategist_score]).reshape(1, 3),
            fmt='%.1f,%.4f,%.4f',
            comments="",
            header="wins,stumbler_score,strategist_score")

    result = (wins / num_episodes), stumbler_score, strategist_score
    if return_none:
        result = None

    return result
