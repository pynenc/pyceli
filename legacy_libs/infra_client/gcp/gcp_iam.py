# pylint: disable=no-member
from dataclasses import dataclass
from typing import Optional, Any

from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed, after_log

import logger

log = logger.get_logger(__name__)


class RetryException(Exception):
    """Exception to indicate tenacity.retry that should try again"""


@dataclass
class IAMClient:
    """Client for GCP IAM API"""

    credentials: service_account.Credentials

    def __post_init__(self) -> None:
        self._api: Optional[Any] = None
        # Cache project_id -> role_name -> Role
        # self._role_cache: dict[str, dict[str, Any]] = defaultdict(dict)

    @property
    def api(self) -> Any:
        """lazy evaluatio of IAM API"""
        if not self._api:
            self._api = discovery.build("iam", "v1", credentials=self.credentials)
        return self._api

    @staticmethod
    def appy_iam_policy_binding(
        api_service: discovery.Resource, role_name: str, required_member: str, resource: str, check_only: bool
    ) -> None:
        """ensure the binding exists in the iamPolicy"""
        # TODO TEST THIS!!!
        try:
            policy: dict = api_service.getIamPolicy(resource=resource).execute()
        except HttpError as ex:
            if ex.status_code not in (404, 403):
                raise
            if check_only:
                log.warning(
                    f"The service account {resource} do not exists yet, the IAM policy to binding role {role_name} to member {required_member} will be executed on deployment"
                )
                if ex.status_code == 403:
                    log.warning(
                        "If warning persist while service iam_policy_binding is present, validate infra_sa has iam.serviceAccounts.getIamPolicy"
                    )
                return

        def set_policy() -> None:
            body = {"policy": policy, "updateMask": "bindings"}
            # https://cloud.google.com/resource-manager/reference/rest/v1/projects/setIamPolicy
            api_service.setIamPolicy(resource=resource, body=body).execute()

        msg = f"IAM policy on resource {resource} binding role {role_name} to member {required_member}"
        policy["bindings"] = policy.get("bindings", [])
        for binding in policy["bindings"]:
            if binding["role"] == role_name:
                if required_member in binding["members"]:
                    log.info(f"Nothing to do, exists {msg}")
                elif check_only:
                    log.warning(f"Necessary to add {msg}")
                else:
                    log.warning(f"Adding {msg}")
                    binding["members"].append(required_member)
                    set_policy()
                return
        if check_only:
            log.warning(f"Role is not binded, {msg} will be added")
        else:
            log.warning(f"Adding new role binding {msg}")
            policy["bindings"].append({"role": role_name, "members": [required_member]})
            set_policy()

    @staticmethod
    def get_project_role_name(role_name: str, project_id: str) -> str:
        """Gets the role name use in GCP"""
        return f"projects/{project_id}/roles/{role_name}"

    @staticmethod
    def get_predefined_role_name(role_name: str) -> str:
        """Gets the role name use in GCP"""
        return f"roles/{role_name}"

    def get_role(self, role_name: str, project_id: str) -> dict:
        """gets the role dict if exists or returns an error"""
        # https://cloud.google.com/iam/docs/reference/rest/v1/projects.roles/get
        return self.api.projects().roles().get(name=self.get_project_role_name(role_name, project_id)).execute()

    def exists_custom_role(self, role_name: str, project_id: str) -> bool:
        """Creates a custom role if necessary"""
        try:
            _ = self.get_role(role_name, project_id)
            return True
        except HttpError as ex:
            if ex.status_code != 404:
                raise
        return False

    def apply_custom_role(self, role_name: str, project_id: str, permissions: list[str], only_check: bool) -> None:
        """Creates a custom role if necessary"""
        try:
            role = self.get_role(role_name, project_id)
            log.info(f"Role {role_name} exists in project {project_id}")
            if role.get("deleted"):
                if only_check:
                    log.warning(f"Role {role_name} is deleted, will be undelted")
                    return
                log.warning(f"Role {role_name} was deleted, undeleting it")
                role = (
                    self.api.projects()
                    .roles()
                    .undelete(name=self.get_project_role_name(role_name, project_id))
                    .execute()
                )
        except HttpError as ex:
            if ex.status_code != 404:
                raise
            if only_check:
                log.warning(f"Role {role_name} will be created in project {project_id} with {permissions=}")
                return
            log.warning(f"Creating role {role_name} in project {project_id} with {permissions=}")
            role = {
                # "name": "storageCreatorRole",
                "title": role_name,
                # https://cloud.google.com/iam/docs/reference/rest/v1/organizations.roles#Role.RoleLaunchStage
                "stage": "GA",
                "description": "Custom role automatially created by infra_client",
                "includedPermissions": permissions,
            }
            body = {"roleId": role_name, "role": role}
            # https://cloud.google.com/iam/docs/reference/rest/v1/projects.roles/create
            self.api.projects().roles().create(parent=f"projects/{project_id}", body=body).execute()
        # https://cloud.google.com/iam/docs/reference/rest/v1/projects.roles/patch
        role_changes: dict[str, Any] = {}
        if role and set(role.get("includedPermissions", [])) != set(permissions):
            role_changes["includedPermissions"] = permissions
        if role and role.get("stage", "ALPHA") != "GA":
            role_changes["stage"] = "GA"
        if role_changes:
            msg = "will change" if only_check else "is changing"
            for key, value in role_changes.items():
                log.warning(f"Role {role_name} {msg} {key} from {role.get(key)} to {value}")
            if not only_check:
                role.update(role_changes)
                self.api.projects().roles().patch(
                    name=self.get_project_role_name(role_name, project_id), body=role
                ).execute()

    @staticmethod
    def get_service_account_email(service_account_name: str, project_id: str) -> str:
        """gets the project/sa email use in IAM"""
        return f"{service_account_name}@{project_id}.iam.gserviceaccount.com"

    @staticmethod
    def get_service_account_project_name(service_account_name: str, project_id: str) -> str:
        """gets the complete project/sa name used in IAM"""
        return f"projects/{project_id}/serviceAccounts/{IAMClient.get_service_account_email(service_account_name, project_id)}"

    def get_service_account(self, service_account_name: str, project_id: str) -> dict:
        """gets a project service account"""
        name = IAMClient.get_service_account_project_name(service_account_name, project_id)
        # https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/get
        return self.api.projects().serviceAccounts().get(name=name).execute()

    @retry(
        # HttpError 429 when requesting https://iam.googleapis.com/v1/projects/pcaas-dev-364815/serviceAccounts?alt=json returned
        # "A quota has been reached for project number 871271722260: Service accounts created per minute per project.
        retry=retry_if_exception_type(RetryException),
        stop=stop_after_attempt(4),
        wait=wait_fixed(15),
        after=after_log(log, logger.logging.WARNING),
    )
    def apply_service_account(self, service_account_name: str, project_id: str, only_check: bool) -> None:
        """create service account if does not exists"""
        try:
            self.get_service_account(service_account_name, project_id)
            log.info(f"Nothing to do, service account {service_account_name} exists in project {project_id}")
        except HttpError as ex:
            if ex.status_code not in (404, 403):
                raise
            if only_check:
                log.warning(f"Service account {service_account_name} doesn't exists in project {project_id}")
                if ex.status_code == 403:
                    log.warning(
                        "If warning persist while service account is present, validate infra_sa has iam.serviceAccounts.get"
                    )
                return
            log.warning(f"Creating Service account {service_account_name} in project {project_id}")
            # https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts#ServiceAccount
            _service_account = {"description": "Service account created by infra_client"}
            body = {"accountId": service_account_name, "serviceAccount": _service_account}
            # https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/create
            try:
                self.api.projects().serviceAccounts().create(name=f"projects/{project_id}", body=body).execute()
            except HttpError as exc:
                if exc.status_code == 429:
                    log.warning(f"Too many request received from Goggle API when trying to create sa: {body}")
                    raise RetryException from exc


## TODO get key from service account
## https://cloud.google.com/iam/docs/creating-managing-service-account-keys#iam-service-account-keys-create-python
## Example from google:
# import os

# from google.oauth2 import service_account
# import googleapiclient.discovery

# def create_key(service_account_email):
#     """Creates a key for a service account."""

#     credentials = service_account.Credentials.from_service_account_file(
#         filename=os.environ['GOOGLE_APPLICATION_CREDENTIALS'],
#         scopes=['https://www.googleapis.com/auth/cloud-platform'])

#     service = googleapiclient.discovery.build(
#         'iam', 'v1', credentials=credentials)

#     key = service.projects().serviceAccounts().keys().create(
#         name='projects/-/serviceAccounts/' + service_account_email, body={}
#         ).execute()

#     # The privateKeyData field contains the base64-encoded service account key
#     # in JSON format.
#     # TODO(Developer): Save the below key {json_key_file} to a secure location.
#     #  You cannot download it again later.
#     # import base64
#     # json_key_file = base64.b64decode(key['privateKeyData']).decode('utf-8')

#     if not key['disabled']:
#         print('Created json key')
