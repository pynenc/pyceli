from typing import Optional, Sequence

from kubernetes import client

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base
from piceli.k8s.templates.deployable import role as role_lib
from piceli.k8s.templates.deployable import role_binding


class ServiceAccount(base.Deployable):
    """Service Account"""

    name: names.Name
    roles: Sequence[role_lib.K8sRole]
    annotations: Optional[dict[str, str]] = None
    labels: Optional[Labels] = None

    def get(
        self,
    ) -> list[
        client.V1Deployment
        | client.V1Role
        | client.V1ClusterRole
        | role_binding.RoleBinding
        | role_binding.ClusterRoleBinding
    ]:
        """gets a Service Account and its related roles and role bindings"""
        obj = client.V1ServiceAccount(
            api_version="v1",
            kind="ServiceAccount",
            metadata=client.V1ObjectMeta(
                name=self.name,
                labels=self.labels,
                annotations=self.annotations,
            ),
        )
        objects = [obj]
        for role in self.roles:
            objects.extend(role.get())
            objects.extend(self.get_role_binding(role).get())
        return objects

    def get_role_binding(
        self, role: role_lib.K8sRole
    ) -> role_binding.RoleBinding | role_binding.ClusterRoleBinding:
        """Gets the service related to this Deployment"""
        if len(name := f"sa-{self.name}-role-{role.name}") > 63:
            if len(name := f"{self.name}-{role.name}") > 63:
                raise ValueError(f"role binding name {name} is too long")
        if isinstance(role, role_lib.ClusterRole):
            return role_binding.ClusterRoleBinding(
                name=name, service_account_name=self.name, role_name=role.name
            )
        return role_binding.RoleBinding(
            name=name, service_account_name=self.name, role_name=role.name
        )
