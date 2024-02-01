# pylint: disable=no-member
from dataclasses import dataclass
from collections import defaultdict
from enum import Enum
import os
from typing import Optional, Any, ClassVar

from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
    after_log,
)

from infra_client.gcp import gcp_constants as const
from infra_client.gcp.gcp_utils import wait
import logger

log = logger.get_logger(__name__)


class RetryException(Exception):
    """Exception to indicate tenacity.retry that should try again"""


@dataclass
class SQLClient:
    """Client for Cloud SQL API"""

    credentials: service_account.Credentials
    IP_RANGE_NAME: ClassVar[str] = "default-ip-range"

    class AvailabilityType(Enum):
        """Cloud SQL Availability type"""

        # ZONAL: The instance serves data from only one zone. Outages in that zone affect data accessibility
        ZONAL = "ZONAL"
        # REGIONAL: The instance can serve data from more than one zone in a region (it is highly available)
        REGIONAL = "REGIONAL"

    class DataDiskType(Enum):
        """The type of data disk"""

        PD_SSD = "PD_SSD"
        PD_HDD = "PD_HDD"

    def __post_init__(self) -> None:
        self._api: Optional[Any] = None
        self._tiers_cache: dict[str, set[str]] = {}

    @property
    def api(self) -> Any:
        """lazy evaluatio of SQL admin API"""
        if not self._api:
            self._api = discovery.build(
                "sqladmin", "v1beta4", credentials=self.credentials
            )
        return self._api

    def reserve_ip_range(self, project_id: str, name: str) -> None:
        """Reserve a static private IP range on the project"""
        compute_service = discovery.build("compute", "v1", credentials=self.credentials)
        # https://cloud.google.com/compute/docs/reference/rest/v1/globalAddresses/list
        addresses = compute_service.globalAddresses().list(project=project_id).execute()
        if not addresses or not any(
            item for item in addresses.get("items", []) if item["name"] == name
        ):
            log.warning(f"Reserving internal IP range '{name}' for Cloud SQL instance")
            body = {
                "name": name,
                "description": f"private ip range reserved for the cloud sql instance in the {project_id=} VPC",
                # "address": "172.30.144.0",
                "prefixLength": 20,
                # "status": "RESERVED",
                # "selfLink": "https://www.googleapis.com/compute/v1/projects/pcaas-dev-364815/global/addresses/default-ip-range",
                # "networkTier": "PREMIUM",
                "addressType": "INTERNAL",
                "purpose": "VPC_PEERING",
                "network": f"https://www.googleapis.com/compute/v1/projects/{project_id}/global/networks/default",
            }
            # https://cloud.google.com/compute/docs/reference/rest/v1/globalAddresses/list
            compute_service.globalAddresses().insert(
                project=project_id, body=body
            ).execute()

            def get_insert_status() -> str:
                address = (
                    compute_service.globalAddresses()
                    .get(project=project_id, address=name)
                    .execute()
                )
                return address["status"]

            wait(
                msg=f"reserve IP address range {name}",
                get_status=get_insert_status,
                stop_condition=lambda x: x == "RESERVED",
                timeout_secs=const.WAIT_RESERVE_STATIC_ADDR_SECS,
                sleep_secs=2,
            )

    def create_network_connection(self, project_id: str) -> None:
        """Create a VPC connection between SQL Cloud and the project VPC Network"""
        # NETWORK PART
        # We connect to the SQL database withing the VPC network of the project
        # 1st check if exists default-ip-range otherwise create it
        service = discovery.build(
            "servicenetworking", "v1", credentials=self.credentials
        )
        parent = "services/servicenetworking.googleapis.com"
        network = f"projects/{project_id}/global/networks/default"
        self.reserve_ip_range(
            project_id, SQLClient.IP_RANGE_NAME
        )  # name must be default-ip-range

        # 2nd connection using those private addresses
        def get_connection() -> Optional[dict]:
            """gets the connection if already exists"""
            res: dict = (
                service.services()
                .connections()
                .list(parent=parent, network=network)
                .execute()
            )
            for conn in res.get("connections", []):
                if SQLClient.IP_RANGE_NAME in conn["reservedPeeringRanges"]:
                    return conn
            return None

        if not get_connection():
            log.warning("Creating new VPC network peering for Cloud SQL instance")
            # https://cloud.google.com/service-infrastructure/docs/service-networking/reference/rest/v1/services.connections#Connection
            body = {
                "network": network,
                "reservedPeeringRanges": [SQLClient.IP_RANGE_NAME],
                "peering": "servicenetworking-googleapis-com",
                "service": "services/servicenetworking.googleapis.com",
            }
            # https://cloud.google.com/service-infrastructure/docs/service-networking/reference/rest/v1/services.connections/create
            service.services().connections().create(parent=parent, body=body).execute()
            wait(
                msg="Connection for Cloud SQL ACTIVE",
                get_status=get_connection,
                stop_condition=lambda x: x is not None,
                timeout_secs=const.WAIT_CREATE_CONN_SECS,
                sleep_secs=5,
                print_status=False,
            )

    def available_tiers(self, project_id: str) -> set[str]:
        """Gets the available Cloud SQL tiers for a project"""
        if project_id not in self._tiers_cache:
            res = self.api.tiers().list(project=project_id).execute()
            self._tiers_cache[project_id] = {t["tier"] for t in res["items"]}
        return self._tiers_cache[project_id]

    def get_necessary_changes(
        self,
        current: dict,
        name: str,
        tier: str,
        region: str,
        availability_type: "SQLClient.AvailabilityType",
        disk_size: int,
        disk_type: "SQLClient.DataDiskType",
        transaction_log_retention_days: int,
        retained_backups_count: int,
    ) -> dict:
        """Check if anything change between current/requested instance and returns that changes"""

        def recursive_defaultdict() -> dict:
            return defaultdict(recursive_defaultdict)

        changes: dict = recursive_defaultdict()
        if (old := current["settings"]["tier"]) != tier:
            changes["settings"]["tier"] = tier
            log.warning(f"Updating instance {name} tier {old} -> {tier}")
        if (old := current["settings"]["availabilityType"]) != availability_type.value:
            changes["settings"]["availabilityType"] = availability_type.value
            msg = (
                "highly available"
                if availability_type == SQLClient.AvailabilityType.REGIONAL
                else "not highly available"
            )
            log.warning(
                f"Updating instance {name} availabilityType to {msg} (up tp 5 min downtime)"
            )
        if (old := current["settings"]["dataDiskType"]) != disk_type.value:
            log.warning(
                f"Not possible to change the disk type ({old} -> {disk_type.value}) of a created instance"
            )
        if (old := current["region"]) != region:
            log.warning(
                f"Not possible to change region ({old} -> {region})  of a created instance"
            )
        if (old := int(current["settings"]["dataDiskSizeGb"])) != disk_size:
            if old > disk_size:
                log.warning(
                    f"Is not possible to decrease the size of a disk ({old} --> {disk_size} GB)"
                )
            else:
                changes["settings"]["dataDiskSizeGb"] = str(disk_size)
                log.warning(
                    f"Increasing instance {name} disk from {old} to {disk_size} GB"
                )
        if (
            old := current["settings"]["backupConfiguration"][
                "backupRetentionSettings"
            ]["retainedBackups"]
        ) != retained_backups_count:
            changes["settings"]["backupConfiguration"]["backupRetentionSettings"][
                "retainedBackups"
            ] = retained_backups_count
            log.warning(
                f"changing number of retained automatic backups from {old} to {retained_backups_count}"
            )
        if (
            old := current["settings"]["backupConfiguration"][
                "transactionLogRetentionDays"
            ]
        ) != transaction_log_retention_days:
            changes["settings"][
                "transactionLogRetentionDays"
            ] = transaction_log_retention_days
            log.warning(
                f"changing transaction retention log from {old} to {transaction_log_retention_days}"
            )
        return changes

    def get_new_instance(
        self,
        project_id: str,
        name: str,
        tier: str,
        region: str,
        availability_type: "SQLClient.AvailabilityType",
        disk_size: int,
        disk_type: "SQLClient.DataDiskType",
        transaction_log_retention_days: int,
        retained_backups_count: int,
    ) -> dict:
        """returns the dict to create a new instance"""
        # https://cloud.google.com/sql/docs/postgres/admin-api/rest/v1beta4/instances#DatabaseInstance
        return {
            # "state": "PENDING_CREATE",
            # - databaseVersion: cannot be changed after instance creation
            #   if we want to change it, create a new database and migrate old instance data
            "databaseVersion": "POSTGRES_14",
            "settings": {
                # - tier: machine type of the instance, changing it restarts the instance
                "tier": tier,
                "availabilityType": availability_type.value,
                "pricingPlan": "PER_USE",
                # "replicationType": "SYNCHRONOUS", # deprecated
                # "activationPolicy": "ALWAYS", # ALWAYS or NEVER(instance is off)
                "ipConfiguration": {
                    "privateNetwork": f"projects/{project_id}/global/networks/default",
                    "authorizedNetworks": [],
                    "ipv4Enabled": False,
                },
                # "locationPreference": {"zone": region + "-a", "secondaryZone": region + "-b"},
                "dataDiskType": disk_type.value,
                # maintenance downtime fixed on Saturdays - 23h (GCP upgrades and other eventual work)
                "maintenanceWindow": {"updateTrack": "stable", "hour": 23, "day": 5},
                "backupConfiguration": {
                    "startTime": "02:00",
                    "location": "eu",
                    "backupRetentionSettings": {
                        "retentionUnit": "COUNT",
                        "retainedBackups": retained_backups_count,
                    },
                    "enabled": True,
                    "replicationLogArchivingEnabled": True,
                    "pointInTimeRecoveryEnabled": True,
                    "transactionLogRetentionDays": transaction_log_retention_days,
                },
                "storageAutoResizeLimit": 0,
                "storageAutoResize": True,
                "dataDiskSizeGb": str(disk_size),
                "deletionProtectionEnabled": True,
                # Enable IAM authorization in the database
                "databaseFlags": [
                    {"name": "cloudsql.iam_authentication", "value": "on"}
                ],
            },
            "project": project_id,
            # "backendType": "SECOND_GEN",
            # "selfLink": "https://sqladmin.googleapis.com/sql/v1beta4/projects/pcaas-dev-364815/instances/pcaas-dev-sql",
            # "connectionName": "pcaas-dev-364815:europe-west6:pcaas-dev-sql",
            "name": name,
            "region": region,
            "rootPassword": os.environ["CLOUD_SQL_ROOT_PASSWORD"],
            # "gceZone": "europe-west6-a",
            # "databaseInstalledVersion": "POSTGRES_14_4",
            # "maintenanceVersion": "POSTGRES_14_4.R20220710.01_16",
            # "createTime": "2023-03-02T16:00:52.335Z",
        }

    def exists(self, name: str, project_id: str) -> bool:
        """check if an instance exists"""
        try:
            self.api.instances().get(project=project_id, instance=name).execute()
            return True
        except HttpError as ex:
            if ex.status_code != 404:
                raise
            return False

    def apply(
        self,
        name: str,
        project_id: str,
        tier: str,
        region: str,
        highly_available: bool,
        disk_size: int,
        disk_type: "SQLClient.DataDiskType",
        transaction_log_retention_days: int,
        retained_backups_count: int,
        only_check: bool,
    ) -> None:
        """Apply the sql database if doesn't exists"""
        # if tier not in (available_tiers := self.available_tiers(project_id)):
        #     raise ValueError(f"{tier=} not available for {project_id=}, {available_tiers=}")
        availability_type = (
            SQLClient.AvailabilityType.REGIONAL
            if highly_available
            else SQLClient.AvailabilityType.ZONAL
        )

        def wait_for_operation(operation: dict) -> None:
            def get_operation() -> str:
                oper = (
                    self.api.operations()
                    .get(project=project_id, operation=operation["name"])
                    .execute()
                )
                return oper["status"] if oper else None

            wait(
                msg=f"Updating the Cloud SQL Instance {name}, {operation=}",
                get_status=get_operation,
                stop_condition=lambda x: x == "DONE",
                timeout_secs=60 * const.WAIT_CLOUD_SQL_INSTANCE_UPDATE_MIN,
                sleep_secs=5,
            )

        try:
            if (
                existing_instance := self.api.instances()
                .get(project=project_id, instance=name)
                .execute()
            ):
                if (
                    operations := self.api.operations()
                    .list(project=project_id, instance=name)
                    .execute()
                ):
                    log.warning(f"There are pending operation on the instance: {name}")
                    for operation in operations.get("items", []):
                        wait_for_operation(operation)
                if update_instance := self.get_necessary_changes(
                    current=existing_instance,
                    name=name,
                    tier=tier,
                    region=region,
                    availability_type=availability_type,
                    disk_size=disk_size,
                    disk_type=disk_type,
                    transaction_log_retention_days=transaction_log_retention_days,
                    retained_backups_count=retained_backups_count,
                ):
                    if only_check:
                        log.warning(
                            f"Cloud SQL instance {name} exits and requires changes: {update_instance}"
                        )
                    else:
                        log.warning(
                            f"Modifying Cloud SQL instance {name}, changes: {update_instance}"
                        )
                        operation = (
                            self.api.instances()
                            .patch(
                                project=project_id, instance=name, body=update_instance
                            )
                            .execute()
                        )
                        wait_for_operation(operation)
                else:
                    log.info(
                        f"Nothing to do, SQL instance {name} already exists and doesn't require changes"
                    )
            return  # if instance doesn't exists and needs to be created, will raise exception:
        except HttpError as ex:
            if ex.status_code != 404:
                raise
            if only_check:
                log.warning(
                    f"Cloud SQL instance {name} don't exits and will be created (up to 25min)"
                )
                return
        log.warning(f"Creating/Updating Cloud SQL instance {name} (up to 25min)")
        self.create_network_connection(project_id)
        # https://cloud.google.com/sql/docs/postgres/admin-api/rest/v1beta4/instances#DatabaseInstance
        # TODO configure instance (get more values we want to configure on lib layer as params)
        # TODO parse the zones to find the locationPreference (instead of hardcode)
        # service = discovery.build("compute", "v1", credentials=self.credentials)
        # service.zones().list(project=project_id).execute()
        instance_body = self.get_new_instance(
            project_id=project_id,
            name=name,
            tier=tier,
            region=region,
            availability_type=availability_type,
            disk_size=disk_size,
            disk_type=disk_type,
            transaction_log_retention_days=transaction_log_retention_days,
            retained_backups_count=retained_backups_count,
        )
        # https://cloud.google.com/sql/docs/postgres/admin-api/rest/v1beta4/instances/insert
        self.api.instances().insert(project=project_id, body=instance_body).execute()

        def get_status() -> str:
            instance = (
                self.api.instances().get(project=project_id, instance=name).execute()
            )
            return instance["state"] if instance else None

        wait(
            msg=f"create/update the Cloud SQL Instance {name}",
            get_status=get_status,
            stop_condition=lambda x: x == "RUNNABLE",
            timeout_secs=60 * const.WAIT_CLOUD_SQL_INSTANCE_UPDATE_MIN,
            sleep_secs=15,
        )

    @retry(
        # HttpError 429 when requesting https://iam.googleapis.com/v1/projects/pcaas-dev-364815/serviceAccounts?alt=json returned
        # "A quota has been reached for project number 871271722260: Service accounts created per minute per project.
        retry=retry_if_exception_type(RetryException),
        stop=stop_after_attempt(4),
        wait=wait_fixed(15),
        after=after_log(log, logger.logging.WARNING),
    )
    def create_user_for_sa(
        self, instance_name: str, project_id: str, sa_name: str, only_check: bool
    ) -> None:
        """creates an user if not already exists for sa_name"""
        msg = f"Cloud SQL user for sa:{sa_name} in instance {instance_name}"
        try:
            _ = (
                self.api.users()
                .get(project=project_id, instance=instance_name, name=sa_name)
                .execute()
            )
            # TODO update user disable or server permissions
            log.warning(f"{msg} exists, nothing to do")
            return
        except HttpError as ex:
            if ex.status_code != 404 and (ex.status_code != 403 and not only_check):
                raise
            if only_check:
                log.warning(f"{msg} doesn't exists and will be created")
                return
        log.warning(f"Creating {msg}")
        # https://cloud.google.com/sql/docs/postgres/admin-api/rest/v1beta4/users#User
        new_user = {
            "project": project_id,
            "instance": instance_name,
            "type": "CLOUD_IAM_SERVICE_ACCOUNT",
            "name": f"{sa_name}@{project_id}.iam",
            "sqlserverUserDetails": {"disable": False, "serverRoles": []},
        }
        try:
            self.api.users().insert(
                project=project_id, instance=instance_name, body=new_user
            ).execute()
        except HttpError as exc:
            if exc.status_code == 429:
                log.warning(
                    f"Too many request received from Goggle API for {project_id=} {instance_name=} {new_user=}"
                )
                raise RetryException from exc
