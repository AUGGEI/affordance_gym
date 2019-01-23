import os
import torch
import torch.optim as optim
from torch.autograd import Variable
from torch.utils import data
from torch.nn import functional as F
import numpy as np

from motion_planning.utils import parse_arguments, GIBSON_ROOT, BEHAVIOUR_ROOT, save_arguments, use_cuda
from motion_planning.utils import plot_loss, plot_scatter, plot_latent_distributions
from motion_planning.perception_policy import end_effector_pose, Predictor
from behavioural_vae.utils import MIN_ANGLE, MAX_ANGLE
from behavioural_vae.ros_monitor import ROSTrajectoryVAE



def load_dataset(perception_name, fixed_camera, debug):


    data_path = os.path.join(GIBSON_ROOT, 'log', perception_name, 'mujoco_latents')
    data_files = os.listdir(data_path)

    # Multiple data packages exist
    latents = []
    camera_distances = []
    azimuths = []
    elevations = []
    target_coords = []

    for file in data_files:
        dataset = np.load(os.path.join(data_path, file))

        latents.append(dataset[0][:, 0, :]) # Bug fix
        camera_distances.append(dataset[1])
        azimuths.append(dataset[2])
        elevations.append(dataset[3])

        target_coords.append(dataset[5])


    # Arrays to numpy
    latents = np.concatenate(latents)
    camera_distances = np.concatenate(camera_distances)
    azimuths = np.concatenate(azimuths)
    elevations = np.concatenate(elevations)
    target_coords = np.concatenate(target_coords)

    # Normalization
    camera_distances = (camera_distances - np.min(camera_distances)) / (np.max(camera_distances) - np.min(camera_distances))
    azimuths = (azimuths - np.min(azimuths)) / (np.max(azimuths) - np.min(azimuths))
    elevations = (elevations - np.min(elevations)) / (np.max(elevations) - np.min(elevations))

    if fixed_camera:

        # The first camera params
        distance = camera_distances[0]
        azimuth = azimuths[0]
        elevation = elevations[0]

        fixed_indices = camera_distances == distance
        fixed_indices = fixed_indices * (elevations == azimuth)
        fixed_indices = fixed_indices * (azimuths == elevation)

        inputs = latents[fixed_indices]
        target_coords = target_coords[fixed_indices]

    else:
        inputs = np.concatenate([latents, camera_distances[:, None], azimuths[:, None], elevations[:, None]], axis=1)

    print(inputs.shape)
    if debug:
        indices = np.random.random_integers(0, inputs.shape[0], 10)
        inputs = inputs[indices]
        target_coords = target_coords[indices]

    # To tensor
    inputs = torch.Tensor(inputs)
    target_coord = torch.Tensor(target_coords)
    return data.TensorDataset(inputs, target_coord)


def main(args):

    save_path = os.path.join('../policy_log', args.policy_name)
    save_arguments(args, save_path)

    device = use_cuda()

    assert(args.model_index > 0)

    action_vae = ROSTrajectoryVAE(args.vae_name, args.latent_dim, args.num_actions,
                                       model_index=args.model_index, num_joints=args.num_joints,  root_path=BEHAVIOUR_ROOT)

    # Trajectory generator
    traj_decoder = action_vae.model.decoder
    traj_decoder.eval()
    traj_decoder.to(device)

    # Load data
    dataset = load_dataset(args.g_name, args.fixed_camera, args.debug)

    # Policy

    if args.fixed_camera:
        policy = Predictor(args.g_latent, args.latent_dim)
    else:
        policy = Predictor(args.g_latent + 3, args.latent_dim)

    policy.to(device)

    optimizer = optim.Adam(policy.parameters(), lr=args.lr)
    optimizer.zero_grad()

    print("Dataset size", dataset.__len__())
    train_size = int(dataset.__len__() * 0.7)
    test_size = dataset.__len__() - train_size

    trainset, testset = data.random_split(dataset, (train_size, test_size))

    train_loader = data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_processes)
    test_loader = data.DataLoader(testset, batch_size=10000)

    best_val = np.inf

    avg_train_losses = []
    avg_val_losses = []


    for epoch in range(args.num_epoch):

        print("Epoch {}".format(epoch + 1))

        policy.train()
        # Training
        train_losses = []
        end_poses = []
        target_poses = []
        latents = []

        for input, target_pose in train_loader:

            # latent1 -> latent2
            latent_1, target_pose = input.to(device), target_pose.to(device)

            latent_2 = policy(Variable(latent_1))

            # latent2 -> trajectory
            trajectories = traj_decoder(latent_2)

            # Reshape to trajectories
            trajectories = action_vae.model.to_trajectory(trajectories)

            # Get the last joint pose
            end_joint_pose = trajectories[:, :, -1]
            # Unnormalize
            end_joint_pose = (MAX_ANGLE - MIN_ANGLE) * end_joint_pose + MIN_ANGLE

            # joint pose -> cartesian
            end_pose = end_effector_pose(end_joint_pose, device)

            loss = F.mse_loss(end_pose, target_pose)

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            train_losses.append(loss.item())
            end_poses.append(end_pose.detach().cpu().numpy())
            target_poses.append(target_pose.cpu().numpy())
            latents.append(latent_2.detach().cpu().numpy())

        avg_loss = np.mean(train_losses)
        avg_train_losses.append(avg_loss)
        print("Average error distance (training) {}".format(np.sqrt(avg_loss)))

        train_poses = np.concatenate(end_poses)
        train_targets = np.concatenate(target_poses)

        # Validation

        policy.eval()
        val_losses = []
        end_poses = []
        target_poses = []

        for latent_1, target_pose in test_loader:

            # latent1 -> latent2
            latent_1, target_pose = latent_1.to(device), target_pose.to(device)
            latent_2 = policy(Variable(latent_1))

            # latent2 -> trajectory
            trajectories = traj_decoder(latent_2)

            # Reshape to trajectories
            trajectories = action_vae.model.to_trajectory(trajectories)

            # Get the last joint pose
            end_joint_pose = trajectories[:, :, -1]

            # Unnormalize
            end_joint_pose = (MAX_ANGLE - MIN_ANGLE) * end_joint_pose + MIN_ANGLE

            # joint pose -> cartesian
            end_pose = end_effector_pose(end_joint_pose, device)

            loss = F.mse_loss(end_pose, target_pose)

            end_poses.append(end_pose.detach().cpu().numpy())

            val_losses.append(loss.item())
            end_poses.append(end_pose.detach().cpu().numpy())
            target_poses.append(target_pose.cpu().numpy())
            latents.append(latent_2.detach().cpu().numpy())

        avg_loss = np.mean(val_losses)
        val_poses = np.concatenate(end_poses)
        val_targets = np.concatenate(target_poses)
        latents = np.concatenate(latents)

        avg_val_losses.append(avg_loss)
        print("Average error distance (validation) {}".format(np.sqrt(avg_loss)))

        if avg_loss < best_val:
            best_val = avg_loss
            torch.save(policy.state_dict(), os.path.join(save_path, 'model.pth.tar'))

        plot_scatter(train_poses, train_targets, os.path.join(save_path, 'train_scatter.png'))
        plot_scatter(val_poses, val_targets, os.path.join(save_path, 'val_scatter.png'))
        poses = np.concatenate([train_poses, val_poses])
        targets = np.concatenate([train_targets, val_targets])
        plot_scatter(poses, targets, os.path.join(save_path, 'full_scatter.png'))
        plot_latent_distributions(latents, os.path.join(save_path, 'latents_distribution.png'))

        plot_loss(avg_train_losses, avg_val_losses, 'Avg mse', os.path.join(save_path, 'avg_mse.png'))
        plot_loss(np.log(avg_train_losses), np.log(avg_val_losses), 'Avg mse in log scale', os.path.join(save_path, 'avg_log_mse.png'))


if __name__ == '__main__':
    main(parse_arguments(behavioural_vae=True, policy=True, gibson=True))
