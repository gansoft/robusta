import logging
import os
import shutil
import subprocess
import textwrap
import threading
from collections import defaultdict
import traceback
import re
from typing import List, Tuple

from ...core.model.env_vars import TARGET_ID, GIT_MAX_RETRIES

GIT_DIR_NAME = "robusta-git"
REPO_LOCAL_BASE_DIR = os.path.join(
    os.environ.get("REPO_LOCAL_BASE_DIR", "/app"), GIT_DIR_NAME
)
SSH_ROOT_DIR = os.environ.get("SSH_ROOT_DIR", "/root/.ssh")


class GitRepoManager:

    manager_lock = threading.Lock()
    repo_map = defaultdict(None)

    @staticmethod
    def get_git_repo(git_repo_url: str, git_key: str, cluster_name: str):
        with GitRepoManager.manager_lock:
            repo = GitRepoManager.repo_map.get(git_repo_url)
            if repo is not None:
                return repo
            repo = GitRepo(git_repo_url, git_key, cluster_name)
            GitRepoManager.repo_map[git_repo_url] = repo
            return repo

    @staticmethod
    def remove_git_repo(git_repo_url):
        with GitRepoManager.manager_lock:
            del GitRepoManager.repo_map[git_repo_url]

    @staticmethod
    def clear_git_repos():
        with GitRepoManager.manager_lock:
            GitRepoManager.repo_map.clear()


class GitRepo:

    initialized: bool = False

    def __init__(self, git_repo_url: str, git_key: str, cluster_name: str):
        GitRepo.init()
        self.key_file_name = self.init_key(git_key)
        self.repo_lock = threading.RLock()
        self.git_repo_url = git_repo_url
        self.cluster_name = cluster_name
        self.repo_name = os.path.splitext(os.path.basename(git_repo_url))[0]
        self.repo_local_path = os.path.join(REPO_LOCAL_BASE_DIR, self.repo_name)
        self.env = os.environ.copy()
        self.env[
            "GIT_SSH_COMMAND"
        ] = f"ssh -i {self.key_file_name} -o IdentitiesOnly=yes"
        self.init_repo()

    def init_key(self, git_key):
        key_file_name = os.path.join(REPO_LOCAL_BASE_DIR, TARGET_ID)
        if os.path.exists(key_file_name):
            return key_file_name

        with open(key_file_name, "w") as key_file:
            key_file.write(textwrap.dedent(f"{git_key}"))
        os.chmod(key_file_name, 0o400)
        if not os.path.exists(SSH_ROOT_DIR):
            os.mkdir(SSH_ROOT_DIR)
        os.system(f"ssh-keyscan -H github.com >> {SSH_ROOT_DIR}/known_hosts")
        return key_file_name

    @staticmethod
    def init():
        if GitRepo.initialized:
            return
        try:
            os.makedirs(REPO_LOCAL_BASE_DIR, exist_ok=True)
        except Exception as e:
            print(
                f"Failed to create git audit base path {REPO_LOCAL_BASE_DIR}",
                traceback.print_exc(),
            )
            raise e
        GitRepo.initialized = True

    def __exec_git_cmd(self, cmd: list[str]):
        shell = False
        if os.name == "nt":
            shell = True

        result = subprocess.run(
            cmd,
            cwd=self.repo_local_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
            env=self.env,
        )
        if result.returncode:
            logging.error(
                f"running command {cmd} failed with returncode={result.returncode}"
            )
            logging.error(f"stdout={result.stdout.decode()}")
            logging.error(f"stderr={result.stderr.decode()}")
            raise Exception(f"Error running git command: {cmd}")

        return result.stdout.decode()

    def init_repo(self):
        with self.repo_lock:
            if os.path.exists(self.repo_local_path):
                logging.info(f"Deleting local repo before init {self.repo_local_path}")
                shutil.rmtree(self.repo_local_path)

            logging.info(
                f"Cloning git repo {self.git_repo_url}. repo name {self.repo_name}"
            )
            os.makedirs(self.repo_local_path, exist_ok=True)
            self.__exec_git_cmd(
                ["git", "clone", self.git_repo_url, self.repo_local_path]
            )
            self.__exec_git_cmd(
                ["git", "config", "--global", "user.email", "runner@robusta.dev"]
            )
            self.__exec_git_cmd(
                ["git", "config", "--global", "user.name", "Robusta Runner"]
            )

    def commit(
        self,
        file_data: str,
        file_path: str,
        file_name,
        commit_message,
    ):
        with self.repo_lock:
            file_local_path = os.path.join(self.repo_local_path, file_path)
            try:
                os.makedirs(file_local_path, exist_ok=True)
                git_file_name = os.path.join(file_local_path, file_name)
                with open(git_file_name, "w") as git_file:
                    git_file.write(file_data)

                self.__exec_git_cmd(["git", "add", git_file_name])
                self.__exec_git_cmd(
                    [
                        "git",
                        "commit",
                        "-m",
                        self.__cluster_commit_msg(commit_message),
                        "--allow-empty",
                    ]
                )
            except Exception as e:
                logging.error(
                    f"Commit file failed {self.repo_local_path} {file_path} {file_name}",
                    traceback.print_exc(),
                )
                GitRepoManager.remove_git_repo(self.git_repo_url)
                raise e

    def __cluster_commit_msg(self, msg: str):
        return f"Cluster {self.cluster_name}::{msg}"

    def push(self):
        with self.repo_lock:
            max_retries = GIT_MAX_RETRIES
            while max_retries > 0:
                try:
                    self.__exec_git_cmd(["git", "push"])
                    return
                except Exception as e:
                    max_retries -= 1
                    if max_retries > 0:
                        self.pull_rebase()
                    else:
                        GitRepoManager.remove_git_repo(self.git_repo_url)
                        logging.error(
                            f"Push failed {self.repo_local_path}", traceback.print_exc()
                        )
                        raise e

    def pull_rebase(self):
        with self.repo_lock:
            self.__exec_git_cmd(["git", "pull", "--rebase", "-Xtheirs"])

    def cluster_changes(
        self, since_minutes: int = 20
    ) -> dict[str, List[Tuple[str, str]]]:
        cluster_changes = defaultdict(list)
        with self.repo_lock:
            self.pull_rebase()
            log = self.__exec_git_cmd(
                ["git", "log", f"--since='{since_minutes} minutes'"]
            )
            commit_date = ""
            for line in log.split("\n"):
                line = line.strip()
                if not line or line.startswith("Author") or line.startswith("commit"):
                    continue
                elif line.startswith("Date"):
                    commit_date = line.replace("Date:", "").strip()
                else:  # this is the commit message
                    if line.startswith("Cluster "):
                        line_suffix = re.sub("Cluster ", "", line)
                        cluster = re.sub("::.*", "", line_suffix)
                        commit_message = re.sub(".*::", "", line_suffix)
                    else:
                        cluster = "Unknown"
                        commit_message = line
                    cluster_changes[cluster].append((commit_date, commit_message))

            return cluster_changes

    def commit_push(
        self, file_data: str, file_path: str, file_name, commit_message: str
    ):
        with self.repo_lock:
            self.commit(file_data, file_path, file_name, commit_message)
            self.push()

    def delete(self, file_path: str, file_name, commit_message: str):
        with self.repo_lock:
            file_local_path = os.path.join(self.repo_local_path, file_path)
            if not os.path.exists(
                file_local_path
            ):  # file not in repo. Might have been added before the audit playbook was configured
                return

            try:
                os.remove(os.path.join(file_local_path, file_name))
                self.__exec_git_cmd(
                    ["git", "commit", "-m", self.__cluster_commit_msg(commit_message)]
                )
            except Exception as e:
                logging.error(
                    f"Commit file failed {self.repo_local_path} {file_path} {file_name}",
                    traceback.print_exc(),
                )
                GitRepoManager.remove_git_repo(self.git_repo_url)
                raise e

    def delete_push(self, file_path: str, file_name, commit_message: str):
        with self.repo_lock:
            self.delete(file_path, file_name, commit_message)
            self.push()
