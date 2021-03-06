# coding: utf-8

import os
import shutil
import tempfile

from six import raise_from

from .consts import \
    DATA_DIR as _DATA_DIR, \
    BACKUP_LOG_FILE as _BACKUP_LOG_FILE, \
    DEFAULT_XLOG_METHOD as _DEFAULT_XLOG_METHOD

from .exceptions import BackupException

from .utils import \
    get_bin_path, \
    default_username as _default_username, \
    execute_utility as _execute_utility


class NodeBackup(object):
    """
    Smart object responsible for backups
    """

    @property
    def log_file(self):
        return os.path.join(self.base_dir, _BACKUP_LOG_FILE)

    def __init__(self,
                 node,
                 base_dir=None,
                 username=None,
                 xlog_method=_DEFAULT_XLOG_METHOD):
        """
        Create a new backup.

        Args:
            node: PostgresNode we're going to backup.
            base_dir: where should we store it?
            username: database user name.
            xlog_method: none | fetch | stream (see docs)
        """

        if not node.status():
            raise BackupException('Node must be running')

        # Set default arguments
        username = username or _default_username()
        base_dir = base_dir or tempfile.mkdtemp()

        # public
        self.original_node = node
        self.base_dir = base_dir
        self.username = username

        # private
        self._available = True

        data_dir = os.path.join(self.base_dir, _DATA_DIR)

        # yapf: disable
        _params = [
            get_bin_path("pg_basebackup"),
            "-p", str(node.port),
            "-h", node.host,
            "-U", username,
            "-D", data_dir,
            "-X", xlog_method
        ]
        _execute_utility(_params, self.log_file)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.cleanup()

    def _prepare_dir(self, destroy):
        """
        Provide a data directory for a copy of node.

        Args:
            destroy: should we convert this backup into a node?

        Returns:
            Path to data directory.
        """

        if not self._available:
            raise BackupException('Backup is exhausted')

        # Do we want to use this backup several times?
        available = not destroy

        if available:
            dest_base_dir = tempfile.mkdtemp()

            data1 = os.path.join(self.base_dir, _DATA_DIR)
            data2 = os.path.join(dest_base_dir, _DATA_DIR)

            try:
                # Copy backup to new data dir
                shutil.copytree(data1, data2)
            except Exception as e:
                raise_from(BackupException('Failed to copy files'), e)
        else:
            dest_base_dir = self.base_dir

        # Is this backup exhausted?
        self._available = available

        # Return path to new node
        return dest_base_dir

    def spawn_primary(self, name=None, destroy=True, use_logging=False):
        """
        Create a primary node from a backup.

        Args:
            name: primary's application name.
            destroy: should we convert this backup into a node?
            use_logging: enable python logging.

        Returns:
            New instance of PostgresNode.
        """

        # Prepare a data directory for this node
        base_dir = self._prepare_dir(destroy)

        # Build a new PostgresNode
        from .node import PostgresNode
        node = PostgresNode(name=name,
                            base_dir=base_dir,
                            use_logging=use_logging)

        # New nodes should always remove dir tree
        node._should_rm_dirs = True

        node.append_conf("postgresql.conf", "\n")
        node.append_conf("postgresql.conf", "port = {}".format(node.port))

        return node

    def spawn_replica(self, name=None, destroy=True, use_logging=False):
        """
        Create a replica of the original node from a backup.

        Args:
            name: replica's application name.
            destroy: should we convert this backup into a node?
            use_logging: enable python logging.

        Returns:
            New instance of PostgresNode.
        """

        # Build a new PostgresNode
        node = self.spawn_primary(name=name,
                                  destroy=destroy,
                                  use_logging=use_logging)

        # Assign it a master and a recovery file (private magic)
        node._assign_master(self.original_node)
        node._create_recovery_conf(username=self.username)

        return node

    def cleanup(self):
        if self._available:
            shutil.rmtree(self.base_dir, ignore_errors=True)
            self._available = False
