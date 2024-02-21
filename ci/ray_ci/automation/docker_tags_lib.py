import json
import subprocess
from datetime import datetime
from typing import List, Optional, Callable
import os
import requests
from dateutil import parser

from ci.ray_ci.utils import logger

bazel_workspace_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY", "")


def _write_to_file(file_path: str, content: List[str]) -> None:
    file_path = os.path.join(bazel_workspace_dir, file_path)
    logger.info(f"Writing to {file_path}......")
    with open(file_path, "w") as f:
        f.write("\n".join(content))


class DockerHubRateLimitException(Exception):
    """
    Exception for Docker Hub rate limit exceeded.
    """

    def __init__(self):
        super().__init__("429: Rate limit exceeded for Docker Hub.")


class RetrieveImageConfigException(Exception):
    """
    Exception for failing to retrieve image config.
    """

    def __init__(self):
        super().__init__("Failed to retrieve image config.")


class AuthTokenException(Exception):
    """
    Exception for failing to retrieve auth token.
    """

    def __init__(self, message: str):
        super().__init__(f"Failed to retrieve auth token from {message}.")


def get_docker_auth_token(namespace: str, repository: str) -> Optional[str]:
    """
    Retrieve Docker Registry token.

    Args:
        namespace: Docker namespace
        repository: Docker repository

    Returns:
        Auth token for Docker.
    """
    service, scope = (
        "registry.docker.io",
        f"repository:{namespace}/{repository}:pull",
    )
    auth_url = f"https://auth.docker.io/token?service={service}&scope={scope}"
    response = requests.get(auth_url)
    if response.status_code != 200:
        raise AuthTokenException(f"Docker. Error code: {response.status_code}")
    token = response.json().get("token", None)
    return token


def get_docker_hub_auth_token():
    """
    Retrieve Docker Hub auth token.
    """
    username = os.environ["DOCKER_HUB_USERNAME"]
    password = os.environ["DOCKER_HUB_PASSWORD"]

    url = "https://hub.docker.com/v2/users/login"
    json_body = {
        "username": username,
        "password": password,
    }
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=json_body)
    if response.status_code != 200:
        raise AuthTokenException(f"Docker Hub. Error code: {response.status_code}")
    return response.json().get("token", None)


def _get_git_log(n_days: int = 30):
    return subprocess.check_output(
        [
            "git",
            "log",
            f"--until='{n_days} days ago'",
            "--pretty=format:%H",
        ],
        text=True,
    )


def list_recent_commit_short_shas(
    n_days: int = 30,
) -> List[str]:
    """
    Get list of recent commit SHAs (short version, first 6 char) on ray master branch.

    Args:
        n_days: Number of days to go back in git log.

    Returns:
        List of recent commit SHAs (6 char).
    """
    commit_shas = _get_git_log(n_days=n_days)
    short_commit_shas = [
        commit_sha[:6] for commit_sha in commit_shas.split("\n") if commit_sha
    ]
    return short_commit_shas


def _call_crane_config(tag: str):
    try:
        return subprocess.check_output(
            ["crane", "config", tag],
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return e.output


def get_image_creation_time(tag: str) -> datetime:
    """
    Get Docker image creation time from tag image config.

    Args:
        tag: Docker tag name

    Returns:
        Datetime object of image creation time.
    """
    result = _call_crane_config(tag=tag)
    if "MANIFEST_UNKNOWN" in result or "created" not in result:
        raise RetrieveImageConfigException()
    config = json.loads(result)
    return parser.isoparse(config["created"])


def delete_tag(tag: str) -> bool:
    """
    Delete tag from Docker Hub repo.

    Args:
        tag: Docker tag name
    Returns:
        True if tag was deleted successfully, False otherwise.
    """
    token = get_docker_hub_auth_token()
    headers = {
        "Authorization": f"Bearer {token}",
    }
    namespace, repo_tag = tag.split("/")
    repository, tag_name = repo_tag.split(":")

    url = f"https://hub.docker.com/v2/repositories/{namespace}/{repository}/tags/{tag_name}"  # noqa E501
    response = requests.delete(url, headers=headers)
    if response.status_code == 429:
        raise DockerHubRateLimitException()
    if response.status_code != 204:
        logger.info(f"Failed to delete {tag}, status code: {response.json()}")
        return False
    else:
        logger.info(f"Deleted tag {tag}")
        return True


def _is_release_tag(
    tag: str,
    release_versions: Optional[List[str]] = None,
) -> bool:
    """
    Check if tag is a release tag & is in the list of release versions.
    Args:
        tag: Docker tag name
        release_versions: List of release versions.
            If None, don't filter by release version.
    Returns:
        True if tag is a release tag and is in the list of release versions.
            False otherwise.
    """
    variables = tag.split(".")
    if len(variables) != 3 and "post1" not in tag:
        return False
    if not variables[0].isnumeric() or not variables[1].isnumeric():
        return False
    if (
        not variables[2].isnumeric()
        and "rc" not in variables[2]
        and "-" not in variables[2]
    ):
        return False

    if "-" in variables[2]:
        variables[2] = variables[2].split("-")[0]
    release_version = ".".join(variables)
    if release_versions and release_version not in release_versions:
        return False

    return True


def _call_crane_cp(tag: str, source: str, aws_ecr_repo: str):
    try:
        with subprocess.Popen(
            [
                "crane",
                "cp",
                source,
                f"{aws_ecr_repo}:{tag}",
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            output = ""
            for line in proc.stdout:
                logger.info(line + "\n")
                output += line
            return_code = proc.wait()
            if return_code:
                raise subprocess.CalledProcessError(return_code, proc.args)
            return output
    except subprocess.CalledProcessError as e:
        return f"Error: {e.output}"


def copy_tag_to_aws_ecr(tag: str, aws_ecr_repo: str) -> bool:
    """
    Copy tag from Docker Hub to AWS ECR.
    Args:
        tag: Docker tag name in format "namespace/repository:tag"
    Returns:
        True if tag was copied successfully, False otherwise.
    """
    _, repo_tag = tag.split("/")
    tag_name = repo_tag.split(":")[1]
    logger.info(f"Copying from {tag} to {aws_ecr_repo}:{tag_name}......")
    result = _call_crane_cp(
        tag=tag_name,
        source=tag,
        aws_ecr_repo=aws_ecr_repo,
    )
    if "Error" in result:
        logger.info(f"Failed to copy {tag} to AWS ECR: {result}")
        return False
    logger.info(f"Copied {tag} to {aws_ecr_repo}:{tag_name}......")
    return True

def _write_to_file(file_path: str, content: List[str]) -> None:
    file_path = os.path.join(bazel_workspace_dir, file_path)
    logger.info(f"Writing to {file_path}......")
    with open(file_path, "w") as f:
        f.write("\n".join(content))


def backup_release_tags(
    namespace: str,
    repository: str,
    release_versions: List[str],
    aws_ecr_repo: str,
    num_tags: int,
) -> None:
    """
    Backup release tags to AWS ECR.
    Args:
        release_versions: List of release versions to backup
        aws_ecr_repo: AWS ECR repository
    """
    docker_hub_tags = query_tags_from_docker_hub(
        filter_func=lambda t: _is_release_tag(t, release_versions),
        namespace=namespace,
        repository=repository,
        num_tags=num_tags,
    )
    _write_to_file("release_tags.txt", docker_hub_tags)
    for t in docker_hub_tags:
        copy_tag_to_aws_ecr(tag=t, aws_ecr_repo=aws_ecr_repo)


def _is_release_tag(
    tag: str,
    release_versions: Optional[List[str]] = None,
) -> bool:
    """
    Check if tag is a release tag & is in the list of release versions.
    Args:
        tag: Docker tag name
        release_versions: List of release versions.
            If None, don't filter by release version.
    Returns:
        True if tag is a release tag and is in the list of release versions.
            False otherwise.
    """
    variables = tag.split(".")
    if len(variables) != 3 and "post1" not in tag:
        return False
    if not variables[0].isnumeric() or not variables[1].isnumeric():
        return False
    if (
        not variables[2].isnumeric()
        and "rc" not in variables[2]
        and "-" not in variables[2]
    ):
        return False

    if "-" in variables[2]:
        variables[2] = variables[2].split("-")[0]
    release_version = ".".join(variables)
    if release_versions and release_version not in release_versions:
        return False

    return True


def _call_crane_cp(tag: str, source: str, aws_ecr_repo: str):
    try:
        with subprocess.Popen(
            [
                "crane",
                "cp",
                source,
                f"{aws_ecr_repo}:{tag}",
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            output = ""
            for line in proc.stdout:
                logger.info(line + "\n")
                output += line
            return_code = proc.wait()
            if return_code:
                raise subprocess.CalledProcessError(return_code, proc.args)
            return output
    except subprocess.CalledProcessError as e:
        return f"Error: {e.output}"


def copy_tag_to_aws_ecr(tag: str, aws_ecr_repo: str) -> bool:
    """
    Copy tag from Docker Hub to AWS ECR.
    Args:
        tag: Docker tag name in format "namespace/repository:tag"
    Returns:
        True if tag was copied successfully, False otherwise.
    """
    _, repo_tag = tag.split("/")
    tag_name = repo_tag.split(":")[1]
    logger.info(f"Copying from {tag} to {aws_ecr_repo}:{tag_name}......")
    result = _call_crane_cp(
        tag=tag_name,
        source=tag,
        aws_ecr_repo=aws_ecr_repo,
    )
    if "Error" in result:
        logger.info(f"Failed to copy {tag} to AWS ECR: {result}")
        return False
    logger.info(f"Copied {tag} to {aws_ecr_repo}:{tag_name}......")
    return True


def query_tags_from_docker_hub(
    filter_func: Callable[[str], bool],
    namespace: str,
    repository: str,
    num_tags: Optional[int] = 1000,
) -> List[str]:
    """
    Query tags from Docker Hub repository with filter.
    If Docker Hub API returns an error, the function will:
        - Stop querying
        - Return the current list of tags.

    Args:
        filter_func: Function to return whether tag should be included.
        namespace: Docker namespace
        repository: Docker repository
        num_tags: Max number of tags to query

    Returns:
        Sorted list of tags from Docker Hub repository
        with format namespace/repository:tag.
    """
    filtered_tags = []
    headers = {
        "Authorization": f"Bearer {get_docker_hub_auth_token()}",
    }

    page_count = 1
    while page_count:
        logger.info(f"Querying page {page_count}")
        url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repository}/tags?page={page_count}&page_size=100"  # noqa E501

        response = requests.get(url, headers=headers)
        response_json = response.json()

        # Stop querying if Docker Hub API returns an error
        if response.status_code != 200:
            logger.info(f"Failed to query tags from Docker Hub: Error: {response_json}")
            return sorted([f"{namespace}/{repository}:{t}" for t in filtered_tags])

        result = response_json["results"]
        tags = [tag["name"] for tag in result]
        filtered_tags_page = list(filter(filter_func, tags))  # Filter tags

        # Add enough tags to not exceed num_tags if num_tags is specified
        if num_tags:
            if len(filtered_tags) + len(filtered_tags_page) > num_tags:
                filtered_tags.extend(
                    filtered_tags_page[: num_tags - len(filtered_tags)]
                )
                break
        filtered_tags.extend(filtered_tags_page)

        logger.info(f"Tag count: {len(filtered_tags)}")
        if not response_json["next"]:
            break
        page_count += 1
    return sorted([f"{namespace}/{repository}:{t}" for t in filtered_tags])


def _call_crane_ls(namespace: str, repository: str):
    try:
        return subprocess.check_output(
            [
                "crane",
                "ls",
                f"{namespace}/{repository}",
            ],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return f"Error: {e.output}"


def query_tags_from_docker_with_crane(namespace: str, repository: str) -> List[str]:
    """
    Query all repo tags from Docker using Crane.

    Args:
        namespace: Docker namespace
        repository: Docker repository

    Returns:
        List of tags from Docker Registry in format namespace/repository:tag.
    """
    get_docker_auth_token(namespace=namespace, repository=repository)
    result = _call_crane_ls(namespace=namespace, repository=repository)
    if "Error" in result:
        raise Exception(f"Failed to query tags from Docker Registry: {result}")
    return [f"{namespace}/{repository}:{t}" for t in result.split("\n")]


def backup_release_tags(
    namespace: str,
    repository: str,
    aws_ecr_repo: str,
    release_versions: Optional[List[str]] = None,
) -> None:
    """
    Backup release tags to AWS ECR.
    Args:
        release_versions: List of release versions to backup
        aws_ecr_repo: AWS ECR repository
    """
    docker_hub_tags = query_tags_from_docker_hub(
        filter_func=lambda t: _is_release_tag(t, release_versions),
        namespace=namespace,
        repository=repository,
    )
    _write_to_file("release_tags.txt", docker_hub_tags)
    for t in docker_hub_tags:
        copy_tag_to_aws_ecr(tag=t, aws_ecr_repo=aws_ecr_repo)
