# Timeout for waiting for a new GKE cluster to be in status RUNNING
WAIT_GKE_RUNNING_MINUTES: int = 20
GKE_NODE_MACHINE_TYPE: str = "e2-highcpu-2"
# Wait for SQL Cloud
WAIT_RESERVE_STATIC_ADDR_SECS: int = 60
WAIT_CREATE_CONN_SECS: int = 120
WAIT_CLOUD_SQL_INSTANCE_UPDATE_MIN: int = 25  # Cloud SQL creation can take 20 min
