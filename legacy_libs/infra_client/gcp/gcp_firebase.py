# pylint: disable=no-member
from dataclasses import dataclass
from typing import Optional, Any

from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError

# from googleapiclient.errors import HttpError
# from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed, after_log

import logger

log = logger.get_logger(__name__)


class RetryException(Exception):
    """Exception to indicate tenacity.retry that should try again"""


@dataclass
class FirebaseClient:
    """Client for GCP Firebase API"""

    credentials: service_account.Credentials

    def __post_init__(self) -> None:
        self._api_firestore: Optional[Any] = None
        self._api_firebase: Optional[Any] = None

    @property
    def api_firestore(self) -> Any:
        """lazy evaluatio of Directory API"""
        if not self._api_firestore:
            self._api_firestore = discovery.build("firebase", "v1beta1", credentials=self.credentials)
        return self._api_firestore

    @property
    def api_firebase(self) -> Any:
        """lazy evaluatio of Directory API"""
        if not self._api_firebase:
            self._api_firebase = discovery.build("firestore", "v1", credentials=self.credentials)
        return self._api_firebase

    def apply(self, project_id: str, region: str, only_check: bool) -> None:
        """Creates a new Firebase project if doesn't exists"""
        request_body = {"locationId": region}
        try:
            self.api_firestore.projects().get(name=f"projects/{project_id}").execute()
            log.info(f"Firebase exists in {project_id=}, nothing to do")
            self.apply_database(project_id, region, only_check)
            return
        except HttpError as ex:
            if ex.status_code != 404:
                raise
            if only_check:
                log.warning(f"Project {project_id} has not a Firebase instance, will be created")
                return
        log.warning(f"Creating Firebase instance for {project_id=}...")
        self.api_firestore.projects().addFirebase(project=f"projects/{project_id}", body=request_body).execute()
        self.apply_database(project_id, region, only_check)
        log.warning(f"Firebase instance for {project_id=} created.")

    def apply_database(self, project_id: str, region: str, only_check: bool) -> None:
        """Enables Firestore (default) DB in the Firebase project in native mode"""
        # default_db = (
        #         self.api_firebase.projects().databases().get(name=f"projects/{project_id}/databases/(default)").execute()
        #     )
        project_path = f"projects/{project_id}"
        default_db_name = "(default)"
        default_db_path = f"{project_path}/databases/{default_db_name}"
        res = self.api_firebase.projects().databases().list(parent=project_path).execute()
        for database in res["databases"]:
            if database["name"] == default_db_path:
                if database["type"] == "FIRESTORE_NATIVE":
                    log.info(f"Firestore database already exist in mode native in {project_id=}, nothing to do")
                elif only_check:
                    log.warning(
                        f"The mode of firestore {default_db_path} would be patches from {database['type']} to native"
                    )
                else:
                    log.warning(f"Updating firestore mode to native on {default_db_path}...")
                    update_mask = "type"
                    body = {"type": "FIRESTORE_NATIVE"}
                    self.api_firebase.projects().databases().patch(
                        name=default_db_path, updateMask=update_mask, body=body
                    ).execute()
                    log.warning(f"Firestore mode UPDATED to native on {default_db_path}...")
                return
        log.warning(f"Creating firestore native instance on {default_db_path}...")
        body = {"type": "FIRESTORE_NATIVE", "locationId": region}
        self.api_firebase.projects().databases().create(
            parent=project_path, databaseId=default_db_name, body=body
        ).execute()
        log.warning(f"Firestore native instance CREATED on {default_db_path}")
