from typing import List, Optional, Union, Dict
import subprocess
import logging
from pathlib import Path
import pkgutil
import contextlib

import sky
from sky.utils import command_runner
from sshtunnel import SSHTunnelForwarder, HandlerSSHTunnelForwarderError
import ray.cloudpickle as pickle

from runhouse.rns.resource import Resource
from runhouse.rns.packages.package import Package
from runhouse.grpc_handler.unary_client import UnaryClient
from runhouse.grpc_handler.unary_server import UnaryService
from runhouse.rh_config import rns_client, open_grpc_tunnels

logger = logging.getLogger(__name__)


class Cluster(Resource):
    RESOURCE_TYPE = "cluster"

    def __init__(self,
                 name,
                 ips: List[str] = None,
                 ssh_creds: Dict = None,
                 dryrun=True,
                 **kwargs  # We have this here to ignore extra arguments when calling from from_config
                 ):
        """
        Args:
            name: Name of the cluster
            dryrun:
        """

        super().__init__(name=name, dryrun=dryrun)

        self.address = ips[0] if ips else None
        self._ssh_creds = ssh_creds
        self.ips = ips
        self._grpc_tunnel = None
        self._secrets_sent = False
        self.client = None

        if not dryrun and self.address:
            # SkyCluster will start ray itself, but will also set address later, so won't reach here.
            self.start_ray()

    @staticmethod
    def from_config(config: dict, dryrun=False):
        # TODO use 'resource_subtype' in config?
        if 'ips' in config:
            return Cluster(**config, dryrun=dryrun)
        else:
            from runhouse.rns.hardware import SkyCluster
            return SkyCluster(**config, dryrun=dryrun)

    @property
    def config_for_rns(self):
        config = super().config_for_rns

        # Also store the ssh keys for the cluster in RNS
        config.update({'ips': self.ips,
                       'ssh_creds': self._ssh_creds
                       })
        # TODO [DG] creds should be shared through secrets management only
        return config

    def is_up(self) -> bool:
        return self.address is not None

    def start_ray(self):
        if self.is_up():
            res = self.run(['ray start --head'], stream_logs=False)
            if res[0] == 0:
                return
            if any(line.startswith('ConnectionError: Ray is trying to start at') for line in res[0][1].splitlines()):
                # Ray is already started
                return
            # Check if ray is installed
            if 'ray' not in self._get_pip_installs(strip_versions=True):
                self.run(['pip install ray'])
                res = self.run(['ray start --head'])
                if not res[0][0]:
                    raise RuntimeError(f'Failed to start ray on cluster <{self.name}>. '
                                       f'Error: {res[0][1]}')
        else:
            raise ValueError(f'Cluster <{self.name}> is not up.')

    def _get_pip_installs(self, strip_versions=False):
        packages = self.run(['pip freeze'], stream_logs=False)[0][1].splitlines()
        if strip_versions:
            packages = [p.split('==')[0] for p in packages]
        return packages

    def sync_runhouse_to_cluster(self, _install_url=None):
        if not self.address:
            raise ValueError(f'No address set for cluster <{self.name}>. Is it up?')
        local_rh_package_path = Path(pkgutil.get_loader('runhouse').path).parent

        # Check if runhouse is installed from source and has setup.py
        if not _install_url and \
                local_rh_package_path.parent.name == 'runhouse' and \
                (local_rh_package_path.parent / 'setup.py').exists():
            # Package is installed in editable mode
            local_rh_package_path = local_rh_package_path.parent
            rh_package = Package.from_string(f'reqs:{local_rh_package_path}', dryrun=True)
            rh_package.to_cluster(self, mount=False)
            status_codes = self.run(['pip install ./runhouse'], stream_logs=True)
        # elif local_rh_package_path.parent.name == 'site-packages':
        else:
            # Package is installed in site-packages
            # status_codes = self.run(['pip install runhouse-nightly==0.0.2.20221202'], stream_logs=True)
            # rh_package = 'runhouse_nightly-0.0.1.dev20221202-py3-none-any.whl'
            # rh_download_cmd = f'curl https://runhouse-package.s3.amazonaws.com/{rh_package} --output {rh_package}'
            # TODO need to check user's current version and install same version?
            _install_url = _install_url or 'runhouse'
            rh_install_cmd = f'pip install {_install_url}'
            status_codes = self.run([rh_install_cmd], stream_logs=True)

        if status_codes[0][0] != 0:
            raise ValueError(f'Error installing runhouse on cluster <{self.name}>')

    def install_packages(self, reqs: List[Union[Package, str]]):
        if not self.is_connected():
            self.connect_grpc()
        to_install = []
        # TODO [DG] validate package strings
        for package in reqs:
            if isinstance(package, str):
                # If the package is a local folder, we need to create the package to sync it over to the cluster
                pkg_obj = Package.from_string(package, dryrun=False)
            else:
                pkg_obj = package

            from runhouse.rns.folders.folder import Folder
            if isinstance(pkg_obj.install_target, Folder) and \
                    pkg_obj.install_target.is_local():
                pkg_str = pkg_obj.name or Path(pkg_obj.install_target.url).name
                logging.info(f'Copying local package {pkg_str} to cluster <{self.name}>')
                remote_package = pkg_obj.to_cluster(self, mount=False, return_dest_folder=True)
                to_install.append(remote_package)
            else:
                to_install.append(package)  # Just appending the string!
        # TODO replace this with figuring out how to stream the logs when we install
        logging.info(f'Installing packages on cluster {self.name}: '
                     f'{[req if isinstance(req, str) else str(req) for req in reqs]}')
        self.client.install_packages(pickle.dumps(to_install))

    def get(self, key, default=None, stream_logs=False):
        if not self.is_connected():
            self.connect_grpc()
        return self.client.get_object(key, stream_logs=stream_logs) or default

    def cancel(self, key, force=False):
        if not self.is_connected():
            self.connect_grpc()
        return self.client.cancel_runs(key, force=force)

    def clear_pins(self, pins: Optional[List[str]] = None):
        if not self.is_connected():
            self.connect_grpc()
        self.client.clear_pins(pins)
        logger.info(f'Clearing pins on cluster {pins or ""}')

    def keep_warm(self, autostop_mins=-1):
        sky.autostop(self.name, autostop_mins, down=True)
        self.autostop_mins = autostop_mins

    # ----------------- gRPC Methods ----------------- #

    def connect_grpc(self, force_reconnect=False):
        # FYI based on: https://sshtunnel.readthedocs.io/en/latest/#example-1
        # FYI If we ever need to do this from scratch, we can use this example:
        # https://github.com/paramiko/paramiko/blob/main/demos/rforward.py#L74
        if not self.address:
            raise ValueError(f'No address set for cluster <{self.name}>. Is it up?')

        # TODO [DG] figure out how to ping to see if tunnel is already up
        if self._grpc_tunnel and force_reconnect:
            self._grpc_tunnel.close()

        # TODO Check if port is already open instead of refcounting?
        # status = subprocess.run(['nc', '-z', self.address, str(self.grpc_port)], capture_output=True)
        # if not self.check_port(self.address, UnaryClient.DEFAULT_PORT):

        tunnel_refcount = 0
        if self.address in open_grpc_tunnels:
            ssh_tunnel, connected_port, tunnel_refcount = open_grpc_tunnels[self.address]
            ssh_tunnel.check_tunnels()
            if ssh_tunnel.tunnel_is_up[ssh_tunnel.local_bind_address]:
                self._grpc_tunnel = ssh_tunnel
        else:
            self._grpc_tunnel, connected_port = self.ssh_tunnel(UnaryClient.DEFAULT_PORT,
                                                                remote_port=UnaryService.DEFAULT_PORT,
                                                                num_ports_to_try=5)
        open_grpc_tunnels[self.address] = (self._grpc_tunnel, connected_port, tunnel_refcount + 1)

        # Connecting to localhost because it's tunneled into the server at the specified port.
        self.client = UnaryClient(host='127.0.0.1', port=connected_port)

    @staticmethod
    def check_port(ip_address, port):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        return s.connect_ex(('127.0.0.1', int(port)))

    def ssh_tunnel(self,
                   local_port,
                   remote_port=None,
                   num_ports_to_try: int = 0) -> (SSHTunnelForwarder, int):
        # Debugging cmds (mac):
        # netstat -vanp tcp | grep 5005
        # lsof -i :5005_
        # kill -9 <pid>

        creds: dict = self.ssh_creds()
        connected = False
        ssh_tunnel = None
        while not connected:
            try:
                if local_port > local_port + num_ports_to_try:
                    raise Exception(f'Failed to create SSH tunnel after {num_ports_to_try} attempts')

                ssh_tunnel = SSHTunnelForwarder(
                    self.address,
                    ssh_username=creds['ssh_user'],
                    ssh_pkey=creds['ssh_private_key'],
                    local_bind_address=('', local_port),
                    remote_bind_address=('127.0.0.1', remote_port or local_port),
                    set_keepalive=1,
                    # mute_exceptions=True,
                )
                ssh_tunnel.start()
                connected = True
            except HandlerSSHTunnelForwarderError as e:
                # try connecting with a different port - most likely the issue is the port is already taken
                local_port += 1
                num_ports_to_try -= 1
                pass

        return ssh_tunnel, local_port

    # TODO [DG] Remove this for now, for some reason it was causing execution to hang after programs completed
    # def __del__(self):
    # if self.address in open_grpc_tunnels:
    #     tunnel, port, refcount = open_grpc_tunnels[self.address]
    #     if refcount == 1:
    #         tunnel.stop(force=True)
    #         open_grpc_tunnels.pop(self.address)
    #     else:
    #         open_grpc_tunnels[self.address] = (tunnel, port, refcount - 1)
    # elif self._grpc_tunnel:  # Not sure why this would be reached but keeping it just in case
    #     self._grpc_tunnel.stop(force=True)

    # import paramiko
    # ssh = paramiko.SSHClient()
    # ssh.load_system_host_keys()
    # ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # from pathlib import Path
    # ssh.connect(self.address,
    #             username=creds['ssh_user'],
    #             key_filename=str(Path(creds['ssh_private_key']).expanduser())
    #             )
    # transport = ssh.get_transport()
    # transport.request_port_forward('', local_port)
    # ssh_tunnel = transport.open_channel("direct-tcpip", ("localhost", local_port),
    #                                     (self.address, remote_port or local_port))
    # if ssh_tunnel.is_active():
    #     connected = True
    #     print(f"SSH tunnel is open to {self.address}:{local_port}")

    def restart_grpc_server(self, _rh_install_url=None, resync_rh=True, restart_ray=False):
        # TODO how do we capture errors if this fails?
        if resync_rh:
            self.sync_runhouse_to_cluster(_install_url=_rh_install_url)
        kill_proc_cmd = f'pkill -f "python3 -m runhouse.grpc_handler.unary_server"'
        grpc_server_cmd = f'screen -dm python3 -m runhouse.grpc_handler.unary_server'
        cmds = [kill_proc_cmd]
        if restart_ray:
            cmds.append('ray stop')
            cmds.append('ray start --head')  # Need to set gpus or Ray will block on cpu-only clusters
        cmds.append(grpc_server_cmd)

        # If we need different commands for debian or ubuntu, we can use this:
        # Need to get actual provider in case provider == 'cheapest'
        # handle = sky.global_user_state.get_cluster_from_name(self.name)['handle']
        # cloud_provider = str(handle.launched_resources.cloud)
        # ubuntu_kill_proc_cmd = f'fuser -k {UnaryService.DEFAULT_PORT}/tcp'
        # debian_kill_proc_cmd = "kill -9 $(netstat -anp | grep 50052 | grep -o '[0-9]*/' | sed 's+/$++')"
        # f'kill -9 $(lsof -t -i:{UnaryService.DEFAULT_PORT})'
        # kill_proc_at_port_cmd = debian_kill_proc_cmd if cloud_provider == 'GCP' \
        #     else ubuntu_kill_proc_cmd

        status_codes = self.run(commands=cmds,
                                stream_logs=True,
                                )
        # As of 2022-27-Dec still seems we need this.
        import time
        time.sleep(2)
        return status_codes

    @contextlib.contextmanager
    def pause_autostop(self):
        pass

    def run_module(self, relative_path, module_name, fn_name, fn_type, args, kwargs):
        if not self.is_connected():
            self.connect_grpc()

        return self.client.run_module(relative_path, module_name, fn_name, fn_type, args, kwargs)

    def is_connected(self):
        return self.client is not None and self.client.is_connected()

    def disconnect(self):
        if self._grpc_tunnel:
            self._grpc_tunnel.stop()
        # if self.client:
        #     self.client.shutdown()

    # ----------------- SSH Methods ----------------- #

    def ssh_creds(self):
        return self._ssh_creds

    def rsync(self, source, dest, up, contents=False):
        """ Note that ending `source` with a slash will copy the contents of the directory into dest,
        while omitting it will copy the directory itself (adding a directory layer)."""
        # FYI, could be useful: https://github.com/gchamon/sysrsync
        if contents:
            source = source + '/'
            dest = dest + '/'
        ssh_credentials = self.ssh_creds()
        runner = command_runner.SSHCommandRunner(self.address, **ssh_credentials)
        runner.rsync(source, dest, up=up, stream_logs=False)

    def ssh(self):
        creds = self.ssh_creds()
        subprocess.run(f"ssh {creds['ssh_user']}:{self.address} -i {creds['ssh_private_key']}".split(' '))

    def run(self, commands: list, stream_logs=True, port_forward=None, require_outputs=True):
        """ Run a list of shell commands on the cluster. """
        # TODO add name parameter to create Run object, and use sky.exec (after updating to sky 2.0):
        # sky.exec(commands, cluster_name=self.name, stream_logs=stream_logs, detach=False)
        runner = command_runner.SSHCommandRunner(self.address, **self.ssh_creds())
        return_codes = []
        for command in commands:
            logger.info(f"Running command on {self.name}: {command}")
            ret_code = runner.run(command,
                                  require_outputs=require_outputs,
                                  stream_logs=stream_logs,
                                  port_forward=port_forward)
            return_codes.append(ret_code)
        return return_codes

    def run_python(self, commands: list, stream_logs=True, port_forward=None):
        """ Run a list of python commands on the cluster. """
        command_str = '; '.join(commands)
        self.run([f'python3 -c "{command_str}"'], stream_logs=stream_logs, port_forward=port_forward)

    def send_secrets(self, reload=False, providers: Optional[List[str]] = None):
        if providers is not None:
            # Send secrets for specific providers from local configs rather than trying to load from Vault
            from runhouse import Secrets
            secrets: list = Secrets.load_provider_secrets(providers=providers)
            # TODO [JL] change this API so we don't have to convert the list to a dict
            secrets: dict = {s['provider']: {k: v for k, v in s.items() if k != 'provider'} for s in secrets}
            load_secrets_cmd = ['import runhouse as rh',
                                f'rh.Secrets.save_provider_secrets(secrets={secrets})']
        elif not self._secrets_sent or reload:
            load_secrets_cmd = ['import runhouse as rh',
                                'rh.Secrets.download_into_env()']
        else:
            # Secrets already sent and not reloading
            return

        self.run_python(load_secrets_cmd, stream_logs=True)
        # TODO [JL] change this to a list to make sure new secrets get sent when the user wants to
        self._secrets_sent = True

    def ipython(self):
        # TODO tunnel into python interpreter in cluster
        pass

    def notebook(self, persist=False, sync_package_on_close=None, port_forward=8888):
        tunnel, port_fwd = self.ssh_tunnel(local_port=port_forward, num_ports_to_try=10)
        try:
            install_cmd = "pip install jupyterlab"
            jupyter_cmd = f'jupyter lab --port {port_fwd} --no-browser'
            # port_fwd = '-L localhost:8888:localhost:8888 '  # TOOD may need when we add docker support
            with self.pause_autostop():
                self.run(commands=[install_cmd, jupyter_cmd], stream_logs=True)

        finally:
            if sync_package_on_close:
                if sync_package_on_close == './':
                    sync_package_on_close = rns_client.locate_working_dir()
                pkg = Package.from_string('local:' + sync_package_on_close)
                self.rsync(source=f'~/{pkg.name}', dest=pkg.local_path, up=False)
            if not persist:
                tunnel.stop(force=True)
                kill_jupyter_cmd = f'jupyter notebook stop {port_fwd}'
                self.run(commands=[kill_jupyter_cmd])
