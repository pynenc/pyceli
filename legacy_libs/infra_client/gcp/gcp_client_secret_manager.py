# pylint: disable=no-member
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import defaultdict
from copy import deepcopy
import secrets
import string
from typing import Optional, Protocol, TypeVar

from google.oauth2 import service_account
from google.api_core.exceptions import NotFound, AlreadyExists
from google.cloud import secretmanager
from google.cloud.secretmanager_v1.types import Secret, SecretVersion
from google.iam.v1.policy_pb2 import Binding
from google.type import expr_pb2
from src.errors import GSMException
from src.util import seconds_to_human_readable
from alerts.teams_client_lite import send_text_alert_only_to_teams
import logger

log = logger.get_logger(__name__)


MAX_VERSIONS_PER_SECRET = 4
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"  # pylint: disable=invalid-name


def generate_random_password() -> str:
    """generates a random secret"""
    password_length = 18
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return "".join(secrets.choice(alphabet) for i in range(password_length))


# fmt: off
# pylint: disable=missing-function-docstring
T = TypeVar('T')
class RepeatedProtocol(Protocol[T]):
    """Mock to type the RepeatedCompositeFieldContainer from protobuf object"""
    def add(self, role: str, members: list[str]) -> None: ...
    def __iter__(self) -> T: ...
    def remove(self, binding: T) -> None: ...
# pylint: enable=missing-function-docstring
# fmt: on


@dataclass
class GSMVersion:
    """Helper to manage GSM Secret class and versions"""

    version_path: str
    version_id: int
    state: SecretVersion.State

    @classmethod
    def from_gsm(cls, version: SecretVersion) -> "GSMVersion":
        """Builds our Helper from GSM SecretVersion instance"""
        return cls(
            version_path=version.name,
            version_id=int(version.name.split("/versions/")[-1]),
            state=version.state,
        )

    @property
    def is_enabled(self) -> bool:
        """The secret version is active and can be accessed"""
        return self.state == SecretVersion.State.ENABLED

    @property
    def is_disabled(self) -> bool:
        """The secret version is temporarily deactivated and cannot be accessed.
        You can re-enable the secret version when needed"""
        return self.state == SecretVersion.State.DISABLED

    @property
    def is_destroyed(self) -> bool:
        """The secret version has been permanently deleted, and the secret data cannot be accessed.
        The metadata, such as the version number, creation timestamp, and state, is retained for auditing purposes
        """
        return self.state == SecretVersion.State.DESTROYED

    def __hash__(self) -> int:
        return hash(self.version_path)


@dataclass
class GSMSecret:
    """Helper to manage GSM Secret class and versions"""

    secret_path: str
    secret_id: str
    versions: list[GSMVersion]

    @classmethod
    def from_gsm(cls, secret: Secret, versions: list[SecretVersion]) -> "GSMSecret":
        """Builds our Helper from GSM a Secret and a its list of SecretVersion"""
        gsm_versions = [GSMVersion.from_gsm(version) for version in versions]
        return cls(
            secret_path=secret.name,
            secret_id=secret.name.split("/secrets/")[-1],
            versions=gsm_versions,
        )

    def has_enabled_versions(self) -> bool:
        """Checks if the secret has any version enabled"""
        return any(v for v in self.versions if v.is_enabled)

    def get_not_destroyed_versions(self) -> list[GSMVersion]:
        """Gets a list of versions that are Enable or Disabled"""
        return [v for v in self.versions if v.is_enabled or v.is_disabled]

    def __hash__(self) -> int:
        return hash(self.secret_path)


@dataclass
class GSMClient:
    """Client for Google Secret manager"""

    credentials: service_account.Credentials

    def __post_init__(self) -> None:
        self._client: Optional[secretmanager.SecretManagerServiceClient] = None
        self._project_secrets_cache: dict[str, dict[str, GSMSecret]] = defaultdict(dict)

    def _refresh_project_secrets(self, project_id: str) -> None:
        for secret in self.client.list_secrets(
            request={"parent": f"projects/{project_id}"}
        ):
            versions = list(
                self.client.list_secret_versions(request={"parent": secret.name})
            )
            gsm_secret = GSMSecret.from_gsm(secret, versions)
            self._project_secrets_cache[project_id][gsm_secret.secret_id] = gsm_secret

    def get_project_secrets(self, project_id: str) -> dict[str, GSMSecret]:
        """Gets a mapping with all the secrets existing in the project: {secret_id: GSMSecret}"""
        if project_id not in self._project_secrets_cache:
            self._refresh_project_secrets(project_id)
        return self._project_secrets_cache[project_id]

    def delete_secret(self, project_id: str, secret: GSMSecret) -> None:
        """Deletes the secret and removes from the cache"""
        self.client.delete_secret(request={"name": secret.secret_path})
        self._project_secrets_cache[project_id].pop(secret.secret_id, None)

    def delete_secret_version(self, version: GSMVersion) -> None:
        """Deletes the secret version and removes it from the secret object"""
        self.client.destroy_secret_version(request={"name": version.version_path})
        version.state = SecretVersion.State.DESTROYED  # type: ignore

    def get_project_secret(
        self, project_id: str, secret_id: str
    ) -> Optional[GSMSecret]:
        """Returns de GSMSecret instace for project_id and secret_id if exists"""
        return self.get_project_secrets(project_id).get(secret_id)

    @property
    def client(self) -> secretmanager.SecretManagerServiceClient:
        """lazy evaluatio of Google Secret Manager client"""
        if not self._client:
            self._client = secretmanager.SecretManagerServiceClient(
                credentials=self.credentials
            )
        return self._client

    def get_secrets_versions_paths(
        self, project_id: str, secret_ids: list[str], version: str = "latest"
    ) -> list[str]:
        """gets a list of paths for each of the secret_ids in the project and version"""
        return [
            self.client.secret_version_path(project_id, sid, version)
            for sid in secret_ids
        ]

    def get_secrets_paths(self, project_id: str, secret_ids: list[str]) -> list[str]:
        """gets a list of paths for each of the secret_ids in the project"""
        return [self.client.secret_path(project_id, sid) for sid in secret_ids]

    def _set_expected_policy_bindings(
        self,
        bindings: RepeatedProtocol[Binding],
        members: list[str],
        secret_id: str,
        only_check: bool,
    ) -> None:
        """Modify (in place) the bindings objects with the expected bindings for the required members"""
        TO_DELETE_ROLES: list[str] = []  # pylint: disable=invalid-name
        TO_DELETE_MEMBERS: set[str] = {  # pylint: disable=invalid-name
            "serviceAccount:pcaas-webtest-apocalyptic-sa@pcaas-web-test.iam.gserviceaccount.com"
        }
        if not bindings:
            log.warning(f"Adding new members {members} to role {ACCESSOR_ROLE}")
            bindings.add(role=ACCESSOR_ROLE, members=members)
            return
        for binding in bindings:
            if ACCESSOR_ROLE not in binding.role:
                if binding.role not in TO_DELETE_ROLES:
                    raise GSMException(
                        f"Unexpected role in {binding}, either add it to the check or to TO_DELETE_ROLES to confirm deletion"
                    )
                if only_check:
                    log.warning(f"The unexpected role in {binding} will be deleted")
                else:
                    bindings.remove(binding)
            elif (
                ACCESSOR_ROLE != binding.role
                and ACCESSOR_ROLE + "_withcond_" in binding.role
            ):
                log.warning(
                    msg := f"Ignoring temporary authorization of {binding.members=} on {secret_id=}"
                )
                send_text_alert_only_to_teams(
                    "There are temporary authorization on Google Secret Manager" + msg
                )
                continue
            elif (existing := set(binding.members)) != (required := set(members)):
                to_delete = existing - required
                new_members = required - existing
                if unauthorize_deletes := to_delete - TO_DELETE_MEMBERS:
                    send_text_alert_only_to_teams(
                        f"Some members:{unauthorize_deletes} in role {binding.role} don't appear in the model and should be deleted, either add them to the permission model or to TO_DELETE_MEMBERS to confirm member deletion"
                    )
                authorized_deletes = to_delete - unauthorize_deletes
                if only_check:
                    log.warning(
                        f"Unexpected members:{to_delete=} will be removed from role {binding.role} and members:{new_members} will be added"
                    )
                    continue
                for to_delete_member in authorized_deletes:
                    log.warning(
                        f"Removing unexpected members {to_delete_member} from role {binding.role}"
                    )
                    binding.members.remove(to_delete_member)
                for to_add_member in new_members:
                    log.warning(
                        f"Adding new member {to_add_member} from role {binding.role}"
                    )
                    binding.members.append(to_add_member)
        return

    def authorize_access_to_typed_member(
        self, project_id: str, secret_id: str, members: list[str], only_check: bool
    ) -> None:
        """Authorize the IAM member (group, user, service account) to access the secrets
        members shuld be typed: ie'serviceAccount:...., group:...., user:.....'
        """
        if not members:
            raise GSMException(
                f"There's no members to authorize on {secret_id=}, that makes the secret useless"
            )
        # member needs to contain the type
        # https://cloud.google.com/sdk/gcloud/reference/alpha/functions/add-iam-policy-binding#REQUIRED-FLAGS
        secret_path = self.client.secret_path(project_id, secret_id)
        try:
            policy = self.client.get_iam_policy(request={"resource": secret_path})
        except NotFound as ex:
            if only_check:
                log.warning(
                    f"Secret {secret_path=} does not exists, will be created before checking IAM policies"
                )
                return
            raise GSMException(f"Secret {secret_path=} does not exists") from ex

        original_bindings = deepcopy(policy.bindings)
        self._set_expected_policy_bindings(
            policy.bindings, members, secret_id, only_check
        )
        # validate policies
        if original_bindings and original_bindings == policy.bindings:
            log.info(
                f"Nothing to do in secret:{secret_path}, contains expected {members=} for accessor role"
            )
        elif only_check:
            log.warning(
                f"Changes are required in secret:{secret_path} bindings:{policy.bindings}, will be applied"
            )
        else:
            new_policy = self.client.set_iam_policy(
                request={"resource": secret_path, "policy": policy}
            )
            log.warning(
                f"Changes applied to secret: {secret_path}, new_policy:{new_policy.bindings}"
            )

    def authorize_temporary_access_to_typed_member(
        self, project_id: str, secret_id: str, member: str, duration_seconds: int
    ) -> None:
        """Authorize the IAM member (group, user, service account) to access the secrets during duration_seconds"""
        secret_path = self.client.secret_path(project_id, secret_id)
        duration_text = seconds_to_human_readable(duration_seconds)
        try:
            policy = self.client.get_iam_policy(request={"resource": secret_path})
        except NotFound as ex:
            raise GSMException(f"Secret {secret_path=} does not exists") from ex
        for binding in policy.bindings:
            if ACCESSOR_ROLE in binding.role:
                if member in binding.members:
                    if ACCESSOR_ROLE == binding.role:
                        log.info(
                            f"Nothing to do member: {member} is already binded"
                            f"to the role{ACCESSOR_ROLE} on secret {secret_id}"
                        )
                        return
                    log.warning(
                        f"Exists a previous condition binding on: {member} is already binded"
                        f"to the role{ACCESSOR_ROLE} on secret {secret_id}, it will be replaced"
                    )
                    policy.bindings.remove(binding)
        expire_time = datetime.now() + timedelta(seconds=duration_seconds)
        policy.version = 3  # set policy version to 3 in order to use conditions
        condition = expr_pb2.Expr(
            title="expires_after",
            description="Expiry time for temporary access",
            expression=f"request.time < timestamp('{expire_time.isoformat()}Z')",
        )
        binding = Binding(role=ACCESSOR_ROLE, members=[member], condition=condition)
        policy.bindings.append(binding)
        log.warning(
            f"Adding temporary {binding.role} to member {member} for {duration_text}"
        )
        new_policy = self.client.set_iam_policy(
            request={"resource": secret_path, "policy": policy}
        )
        log.info(
            f"The policy of the secret {secret_id} has been modified {new_policy=}"
        )

    def exists_secret_and_has_enabled_versions(
        self, project_id: str, secret_id: str
    ) -> bool:
        """Checks if the secret already exists and has at least one version enabled"""
        if secret := self.get_project_secret(project_id, secret_id):
            return secret.has_enabled_versions()
        return False

    def create_secrets_if_not_exists(
        self, project_id: str, secret_ids: set[str], only_check: bool
    ) -> None:
        """create the secrets if do not exists already"""
        for secret_id in secret_ids:
            if not self.exists_secret_and_has_enabled_versions(project_id, secret_id):
                if only_check:
                    log.warning(
                        f"{secret_id=} does not exists (or has no versions) in {project_id=} and will be created/initialized"
                    )
                    return
                log.warning(f"Creating {secret_id=} on {project_id=}")
                try:
                    self.client.create_secret(
                        request={
                            "parent": f"projects/{project_id}",
                            "secret_id": secret_id,
                            "secret": {"replication": {"automatic": {}}},
                        }
                    )
                except AlreadyExists:
                    log.warning(
                        f"Secret {secret_id=} existed without any version (probably unexpected retry)"
                    )
                if secret_id.endswith("__POSTGRES_PASSWORD"):
                    log.warning(
                        "postgres_pwd already existed before GSM, needs to be reused first time GSM runs"
                    )
                    # TODO! once deployed in all the clusters DELETE THIS!!!
                    import os  # pylint: disable=import-outside-toplevel

                    password = os.environ["POSTGRES_PASSWORD"]
                elif secret_id.endswith("CLOUD_SQL_ROOT_PASSWORD"):
                    import os  # pylint: disable=import-outside-toplevel

                    password = os.environ["CLOUD_SQL_ROOT_PASSWORD"]
                else:
                    password = generate_random_password()
                payload = password.encode("UTF-8")
                self.client.add_secret_version(
                    request={
                        "parent": self.client.secret_path(project_id, secret_id),
                        "payload": {"data": payload},
                    }
                )
                log.warning(f"new secret version added for {secret_id}")

    def clean_up(
        self,
        project_id: str,
        expected_secret_ids: set[str],
        to_delete_secret_ids: set[str],
        only_check: bool,
    ) -> None:
        """This function takes care of keeping only required secret/versions in GSM
        - If there is a secret in GSM that do not exists in expected_secret_ids
           -> if is in to_delete_secret_ids: will delete it
           -> otherise: will raise an exception
        - If an expected secret has more than MAX_VERSIONS_PER_SECRET will delete the old versions
        """
        for secret in list(self.get_project_secrets(project_id).values()):
            msg = f"secret {secret.secret_id} on {project_id=}"
            if secret.secret_id not in expected_secret_ids:
                if secret.secret_id in to_delete_secret_ids:
                    if only_check:
                        log.warning(f"Unexpected {msg}, will be deleted")
                    else:
                        log.warning(f"Deleting unexpected {msg}")
                        self.delete_secret(project_id, secret)
                else:
                    raise GSMException(
                        f"Unexpected {msg}, "
                        "ANY GSM Secret SHOULD EXISTS IN THE permission_model "
                        "(either granted to a service account or marked for deletion at SECRET_IDS_FOR_DELETION)"
                    )
            else:
                if (
                    len(versions := secret.get_not_destroyed_versions())
                    > MAX_VERSIONS_PER_SECRET
                ):
                    delete_count = len(secret.versions) - MAX_VERSIONS_PER_SECRET
                    if only_check:
                        log.warning(
                            f"{msg} has more than {MAX_VERSIONS_PER_SECRET} enabled/disabled(not destroyed) versions"
                            f"first {delete_count} will be destroyed:"
                            f"versions {versions[:delete_count]} from {versions}"
                        )
                    else:
                        for version in versions[:delete_count]:
                            log.warning(
                                f"Destroying version {version.version_id} from {msg}"
                            )
                            self.delete_secret_version(version)
