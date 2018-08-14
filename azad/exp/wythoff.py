import os, csv
import sys

import errno
import pudb

from collections import defaultdict
from copy import deepcopy

import torch
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

from azad.util.wythoff import peek
from azad.util.wythoff import pad_board
from azad.util.wythoff import flatten_board
from azad.util.wythoff import convert_ijv
from azad.util.wythoff import balance_ijv
from azad.util.wythoff import evaluate_models
from azad.util.wythoff import estimate_cold
from azad.util.wythoff import estimate_hot
from azad.util.wythoff import estimate_hot_cold
from azad.util.wythoff import estimate_alp_hot_cold
from azad.util.wythoff import estimate_strategic_value
from azad.util.wythoff import create_env
from azad.util.wythoff import create_moves
from azad.util.wythoff import create_bias_board
from azad.util.wythoff import create_all_possible_moves
from azad.util.wythoff import create_cold_board
from azad.util.wythoff import np_plot_wythoff_max_values
from azad.util.wythoff import plot_cold_board
from azad.util.wythoff import plot_wythoff_board
from azad.util.wythoff import plot_wythoff_expected_values


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
                                memory_size=2000,
                                cold_threshold=0.0,
                                hot_threshold=0.5,
                                tensorboard=None,
                                update_every=5,
                                seed=None,
                                save=False,
                                debug=False):
    """Learn Wythoff's with a stumbler-strategist network"""

    # -----------------------------------------------------------------------
    # Init

    # Game sizes
    m, n, _, _ = peek(create_env(strategist_game))
    o, p, _, _ = peek(create_env(stumbler_game))

    # Agents, etc
    stumbler_pair = (None, None)
    strategist = None
    bias_board = None
    influence = 0.0

    # ------------------------------------------------------------------------
    for episode in range(num_episodes):

        # Stumbler
        stumbler_pair = wythoff_stumbler(
            num_episodes=num_stumbles,
            game=stumbler_game,
            epsilon=epsilon,
            gamma=gamma,
            learning_rate=learning_rate_stumbler,
            model=stumbler_pair[0],
            opponent=stumbler_pair[1],
            bias_board=bias_board,
            influence=influence,
            tensorboard=tensorboard,
            update_every=update_every,
            debug=debug,
            seed=seed)

        # Strategist
        player = stumbler_pair[0]
        strategist, bias_board, influence = wythoff_strategist(
            player,
            stumbler_game,
            num_episodes=num_strategies,
            game=strategist_game,
            cold_threshold=cold_threshold,
            hot_threshold=hot_threshold,
            learning_rate=learning_rate_strategist,
            memory_size=memory_size,
            tensorboard=tensorboard,
            update_every=update_every,
            debug=debug,
            seed=seed)

        # --------------------------------------------------------------------
        if save and (int(episode) % update_every) == 0:
            state = {
                'episode': episode,
                'epsilon': epsilon,
                'anneal': anneal,
                'gamma': gamma,
                'num_episodes': num_episodes,
                'num_stumbles': num_stumbles,
                'num_strategies': num_strategies,
                'influence': influence,
                'stumbler_game': stumbler_game,
                'strategist_game': strategist_game,
                'cold_threshold': cold_threshold,
                'hot_threshold': hot_threshold,
                'learning_rate_stumbler': learning_rate_stumbler,
                'learning_rate_strategist': learning_rate_strategist,
                'strategist_state_dict': strategist.state_dict(),
                'stumbler_player_dict': stumbler_pair[0].items(),
                'stumbler_opponent_dict': stumbler_pair[1].items()
            }
            torch.save(
                state, os.path.join(save,
                                    "stumber_strategist_network.pytorch"))

            # Save board images
            plot_wythoff_expected_values(
                o, p, stumbler_pair[0], vmin=-2, vmax=2, path=save)
            est_hc_board = estimate_hot_cold(
                o,
                p,
                stumbler_pair[0],
                hot_threshold=hot_threshold,
                cold_threshold=cold_threshold)
            plot_wythoff_board(
                est_hc_board, path=save, name='est_hc_board.png')
            plot_wythoff_board(
                bias_board, vmin=-1, vmax=0, path=save, name='bias_board.png')

    return stumbler_pair, strategist


def apply_bias_board(Qs, bias_board, influence):
    return Qs


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
                     tensorboard=None,
                     update_every=5,
                     debug=False,
                     seed=None):
    """Train a Q-agent to play Wythoff's game, using a lookup table."""
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
    env = create_env(game)
    env.seed(seed)
    np.random.seed(seed)

    # ------------------------------------------------------------------------
    # Build a Q agent, and its optimizer
    default_Q = 0.0
    m, n, board, available = peek(env)

    # Init the lookup tables?
    if model is None:
        model = {}
    if opponent is None:
        opponent = {}

    # Log fraction of optimal PLAYER moves
    optim = 0.0

    # ------------------------------------------------------------------------
    for episode in range(num_episodes):
        # Re-init
        steps = 1

        x, y, board, available = env.reset()
        board = tuple(flatten_board(board).numpy())
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
        # Play a game!
        done = False
        player_win = False
        while not done:
            # PLAYER CHOOSES A MOVE
            try:
                move_i = epsilon_greedy(
                    model[board], epsilon=epsilon_e, mode='numpy')
            except KeyError:
                model[board] = np.ones(len(available)) * default_Q
                move_i = np.random.randint(0, len(available))
            move = available[move_i]

            # Analyze it...
            best = 0.0
            if cold_move_available(x, y, available):
                if move in locate_cold_moves(x, y, available):
                    best = 1.0
                optim += (best - optim) / (episode + 1)

            # PLAY THE MOVE
            (x, y, board, available), reward, done, _ = env.step(move)
            board = tuple(flatten_board(board).numpy())
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
                    move_i = epsilon_greedy(
                        opponent[board], epsilon=epsilon_e, mode='numpy')
                except KeyError:
                    opponent[board] = np.ones(len(available)) * default_Q
                    move_i = np.random.randint(0, len(available))
                move = available[move_i]

                # PLAY THE MOVE
                (x, y, board, available), reward, done, _ = env.step(move)
                board = tuple(flatten_board(board).numpy())
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
        # Learn by unrolling the game...

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
            writer.add_scalar(os.path.join(tensorboard, 'reward'), r, episode)
            writer.add_scalar(os.path.join(tensorboard, 'Q'), Q, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'error'), loss, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'steps'), steps, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'optimal'), optim, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'epsilon'), epsilon_e, episode)

            # Cold ref:
            plot_cold_board(m, n, path=tensorboard, name='cold_board.png')
            writer.add_image(
                'cold_positions',
                skimage.io.imread(os.path.join(tensorboard, 'cold_board.png')))

            # Agent max(Q) boards
            np_plot_wythoff_max_values(
                m, n, model, path=tensorboard, name='player_max_values.png')
            writer.add_image(
                'player',
                skimage.io.imread(
                    os.path.join(tensorboard, 'player_max_values.png')))

            np_plot_wythoff_max_values(
                m,
                n,
                opponent,
                path=tensorboard,
                name='opponent_max_values.png')
            writer.add_image(
                'opponent',
                skimage.io.imread(
                    os.path.join(tensorboard, 'opponent_max_values.png')))

    # ------------------------------------------------------------------------
    # The end
    if tensorboard is not None:
        writer.close()

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
                     tensorboard=None,
                     update_every=5,
                     debug=False,
                     seed=None):
    """Train a Q-agent to play Wythoff's game, using a lookup table."""
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
    env = create_env(game)
    env.seed(seed)
    np.random.seed(seed)

    # ------------------------------------------------------------------------
    # Build a Q agent, and its optimizer
    default_Q = 0.0
    m, n, board, available = peek(env)
    all_possible_moves = create_all_possible_moves(m, n)

    # Init the lookup tables?
    if model is None:
        model = {}
    if opponent is None:
        opponent = {}

    # Log fraction of optimal PLAYER moves
    optim = 0.0

    # ------------------------------------------------------------------------
    for episode in range(num_episodes):
        # Re-init
        steps = 1

        x, y, board, available = env.reset()
        board = tuple(flatten_board(board).numpy())
        moves_index = locate_moves(available, all_possible_moves)

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
        t_moves_index = [moves_index]
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
        # Play a game!
        done = False
        player_win = False
        while not done:
            # PLAYER CHOOSES A MOVE
            moves_index = locate_moves(available, all_possible_moves)
            if debug:
                print(">>> All moves {}".format(all_possible_moves))
                print(">>> Moves index {}".format(moves_index))
                print(">>> Available {}".format(available))

            try:
                move_i = epsilon_greedy(
                    model[board],
                    epsilon=epsilon_e,
                    index=moves_index,
                    mode='numpy')
            except KeyError:
                # Init an empty movetable
                model[board] = np.ones(len(all_possible_moves)) * default_Q
                # Pick a random move
                move_i = np.random.randint(0, len(available))
                move_i = moves_index[move_i]

            move = all_possible_moves[move_i]

            # ANALYZE it...
            best = 0.0
            if cold_move_available(x, y, available):
                if move in locate_cold_moves(x, y, available):
                    best = 1.0
                optim += (best - optim) / (episode + 1)

            # PLAY THE MOVE
            (x, y, board, available), reward, done, _ = env.step(move)
            board = tuple(flatten_board(board).numpy())
            steps += 1

            # Log....
            if debug:
                print(">>> PLAYER move {}".format(move))

            t_state.append(board)
            t_move.append(move)
            t_available.append(available)
            t_move_i.append(move_i)
            t_moves_index.append(moves_index)
            t_reward.append(reward)

            if done:
                player_win = True
                t_state.append(board)
                t_move.append(move)
                t_available.append(available)
                t_move_i.append(move_i)
                t_moves_index.append(moves_index)
                t_reward.append(reward)

            # ----------------------------------------------------------------
            if not done:
                # OPPONENT CHOOSES A MOVE
                moves_index = locate_moves(available, all_possible_moves)
                try:
                    move_i = epsilon_greedy(
                        opponent[board],
                        epsilon=epsilon_e,
                        index=moves_index,
                        mode='numpy')
                except KeyError:
                    # Init an empty movetable
                    opponent[board] = np.ones(
                        len(all_possible_moves)) * default_Q
                    # Pick a random move
                    move_i = np.random.randint(0, len(available))
                    move_i = moves_index[move_i]

                move = all_possible_moves[move_i]

                # PLAY THE MOVE
                (x, y, board, available), reward, done, _ = env.step(move)
                board = tuple(flatten_board(board).numpy())
                steps += 1

                # Log....
                if debug:
                    print(">>> OPPONENT move {}".format(move))
                    print(">>> Available {}".format(available))

                t_state.append(board)
                t_move.append(move)
                t_available.append(available)
                t_move_i.append(move_i)
                t_moves_index.append(moves_index)
                t_reward.append(reward)

                if done:
                    t_state.append(board)
                    t_move.append(move)
                    t_available.append(available)
                    t_move_i.append(move_i)
                    t_moves_index.append(moves_index)
                    t_reward.append(reward)

        # ----------------------------------------------------------------
        # Learn by unrolling the game...

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
                max_Q = model[next_s][t_moves_index[i]].max()
            except KeyError:
                model[next_s] = np.ones(len(all_possible_moves)) * default_Q
                max_Q = model[next_s][t_moves_index[i]].max()

            if player_win:
                r = t_reward[i]
            else:
                r = -1 * t_reward[i + 1]

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
                max_Q = opponent[next_s][t_moves_index[i]].max()
            except KeyError:
                opponent[next_s] = np.ones(len(all_possible_moves)) * default_Q
                max_Q = opponent[next_s][t_moves_index[i]].max()

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
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_reward'), r, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_Q'), Q, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_error'), loss, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_steps'), steps, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_optimal'), optim, episode)
            writer.add_scalar(
                os.path.join(tensorboard, 'stumbler_epsilon'), epsilon_e,
                episode)

            # Cold ref:
            plot_cold_board(m, n, path=tensorboard, name='cold_board.png')
            writer.add_image(
                'cold_positions',
                skimage.io.imread(os.path.join(tensorboard, 'cold_board.png')))

            # Agent max(Q) boards
            np_plot_wythoff_max_values(
                m, n, model, path=tensorboard, name='player_max_values.png')
            writer.add_image(
                'player',
                skimage.io.imread(
                    os.path.join(tensorboard, 'player_max_values.png')))

            np_plot_wythoff_max_values(
                m,
                n,
                opponent,
                path=tensorboard,
                name='opponent_max_values.png')
            writer.add_image(
                'opponent',
                skimage.io.imread(
                    os.path.join(tensorboard, 'opponent_max_values.png')))

    # ------------------------------------------------------------------------
    # The end
    if tensorboard is not None:
        writer.close()

    return model, opponent


def wythoff_strategist(stumbler_model,
                       stumbler_game,
                       num_episodes=1000,
                       cold_threshold=0.0,
                       hot_threshold=0.5,
                       learning_rate=0.01,
                       memory_size=2000,
                       game='Wythoff50x50',
                       tensorboard=None,
                       update_every=50,
                       debug=False,
                       seed=None):
    """Learn a heuristic for Wythoffs by sampling Q values."""

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
    env = create_env(game)
    env.seed(seed)
    np.random.seed(seed)

    m, n, board, _ = peek(env)
    all_possible_moves = create_all_possible_moves(m, n)

    # Working mem size
    batch_size = 64

    # Peek at stumbler env
    o, p, _, _ = peek(create_env(stumbler_game))

    # Init the strategist net
    num_hidden1 = 100
    num_hidden2 = 25
    model = HotCold3(2, num_hidden1=num_hidden1, num_hidden2=num_hidden2)
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)
    memory = ReplayMemory(memory_size)

    # -----------------------------------------------------------------------
    for episode in range(num_episodes):
        if debug:
            print("---------------------------------------")
            print(">>> STRATEGIST ({}).".format(episode))

        # Extract strategic data from the stumber,
        # project it and remember that
        strategic_default_value = 0.0
        strategic_value = estimate_cold(
            o, p, stumbler_model, threshold=cold_threshold)

        # strategic_value = estimate_hot_cold(
        #     o, p, stumbler_model, hot_threshold=0.5, cold_threshold=0.0)

        # ...Into tuples
        s_data = convert_ijv(strategic_value)
        s_data = balance_ijv(s_data, strategic_default_value)
        if s_data is not None:
            for d in s_data:
                memory.push(*d)

        loss = 0.0
        if len(memory) > batch_size:
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
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if debug:
                print(">>> Coords: {}".format(coords))
                print(">>> Values: {}".format(values))
                print(">>> Predicted values: {}".format(values))
                print(">>> Loss {}".format(loss))

                print(">>> Last win {}".format(win))
                print(">>> Influence {}".format(influence))

        # --------------------------------------------------------------------
        # Use the trained strategist to generate a bias_board,
        bias_board = create_bias_board(m, n, model)

        # Est performance. Count strategist wins.
        win = evaluate_models(
            stumbler_model,
            model,
            stumbler_env,
            env,
            num_eval=num_evals,
            debug=debug)

        # Update the influence and then the bias_board
        if win > 0.5:
            influence += learning_rate
        else:
            influence -= learning_rate
        influence = np.clip(influence, 0, 1)

        # --------------------------------------------------------------------
        if tensorboard and (int(episode) % update_every) == 0:
            # Timecourse
            writer.add_scalar(
                os.path.join(path, 'stategist_error'), loss, episode)
            writer.add_scalar(
                os.path.join(path, 'Stategist_wins'), win, episode)
            writer.add_scalar(
                os.path.join(path, 'Stategist_influence'), influence, episode)

            plot_wythoff_board(
                bias_board, vmin=-1, vmax=0, path=path, name='bias_board.png')
            writer.add_image(
                'strategist_learned_board',
                skimage.io.imread(os.path.join(path, 'bias_board.png')))

    # ------------------------------------------------------------------------
    # The end
    if tensorboard:
        writer.close()

    return (model, bias_board, influence)


def wythoff_optimal(path,
                    num_episodes=1000,
                    learning_rate=0.01,
                    num_hidden1=100,
                    num_hidden2=25,
                    stumbler_game='Wythoff10x10',
                    strategist_game='Wythoff50x50',
                    tensorboard=False,
                    debug=False,
                    seed=None):
    """An optimal stumbler teaches the strategist."""

    # ------------------------------------------------------------------------
    # Setup
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise

    m, n, board, _ = peek(create_env(strategist_game))
    o, p, _, _ = peek(create_env(stumbler_game))

    if debug:
        print(">>> TRANING AN OPTIMAL STRATEGIST.")
        print(">>> Train board {}".format(o, p))
        print(">>> Test board {}".format(m, n))

    # Log setup
    if tensorboard:
        writer = SummaryWriter(log_dir=path)

    # Seeding...
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

        if tensorboard and (int(episode) % 50) == 0:
            writer.add_scalar(os.path.join(path, 'error'), loss, episode)

            plot_wythoff_board(
                strategic_value,
                vmin=0,
                vmax=1,
                path=path,
                name='strategy_board.png')
            writer.add_image(
                'Training board',
                skimage.io.imread(os.path.join(path, 'strategy_board.png')))

            plot_wythoff_board(
                bias_board, vmin=0, vmax=1, path=path, name='bias_board.png')
            writer.add_image(
                'Testing board',
                skimage.io.imread(os.path.join(path, 'bias_board.png')))

    # The end
    if tensorboard:
        writer.close()

    return model, env,
