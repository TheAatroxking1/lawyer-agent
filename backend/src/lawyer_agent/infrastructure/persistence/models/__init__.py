from lawyer_agent.infrastructure.persistence.models.ai_jobs import (
    AIJobAccessGrantModel,
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobInboxModel,
    AIJobModel,
    AIJobOutboxModel,
    AIJobStepEffectModel,
    MessageSecurityRejectionModel,
    PermissionFeatureRolloutModel,
    PermissionFeatureTenantStateModel,
)
from lawyer_agent.infrastructure.persistence.models.audit import (
    AuditEventModel,
    IdempotencyRecordModel,
)
from lawyer_agent.infrastructure.persistence.models.authorization import (
    MembershipRoleAssignmentModel,
    PermissionModel,
    PlatformRoleAssignmentModel,
    PlatformRoleModel,
    PlatformRolePermissionModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
    TenantInvitationRoleAssignmentModel,
    TenantRoleModel,
    TenantRolePermissionModel,
)
from lawyer_agent.infrastructure.persistence.models.identity import (
    AuthIdentityModel,
    IdentityVerificationChallengeModel,
    PasswordCredentialModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
    LegalDatasetSnapshotModel,
    LegalInstrumentModel,
    LegalLoadBatchModel,
    LegalProvisionModel,
    LegalQualityIssueModel,
    LegalVersionModel,
)
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantDocumentModel,
    TenantDocumentVersionModel,
    TenantMatterModel,
    TenantMatterPartyModel,
)
from lawyer_agent.infrastructure.persistence.models.outbox import (
    AuthorizationCacheInvalidationOutboxModel,
)
from lawyer_agent.infrastructure.persistence.models.sessions import (
    AuthSessionModel,
    RefreshTokenRecordModel,
)
from lawyer_agent.infrastructure.persistence.models.tenancy import (
    DepartmentModel,
    TenantInvitationModel,
    TenantMembershipModel,
    TenantModel,
)

__all__ = [
    "AIJobAccessGrantModel",
    "AIJobAttemptModel",
    "AIJobExecutionGrantModel",
    "AIJobInboxModel",
    "AIJobModel",
    "AIJobOutboxModel",
    "AIJobStepEffectModel",
    "AuditEventModel",
    "AuthorizationCacheInvalidationOutboxModel",
    "AuthIdentityModel",
    "AuthSessionModel",
    "DepartmentModel",
    "IdempotencyRecordModel",
    "IdentityVerificationChallengeModel",
    "LegalChunkModel",
    "LegalDatasetSnapshotModel",
    "LegalInstrumentModel",
    "LegalLoadBatchModel",
    "LegalProvisionModel",
    "LegalQualityIssueModel",
    "LegalVersionModel",
    "MembershipRoleAssignmentModel",
    "MessageSecurityRejectionModel",
    "PasswordCredentialModel",
    "PermissionFeatureRolloutModel",
    "PermissionFeatureTenantStateModel",
    "PermissionModel",
    "PlatformRoleAssignmentModel",
    "PlatformRoleModel",
    "PlatformRolePermissionModel",
    "RefreshTokenRecordModel",
    "RoleTemplateModel",
    "RoleTemplatePermissionModel",
    "TenantInvitationModel",
    "TenantInvitationRoleAssignmentModel",
    "TenantMembershipModel",
    "TenantModel",
    "TenantRoleModel",
    "TenantRolePermissionModel",
    "UserModel",
    "TenantDocumentModel",
    "TenantDocumentVersionModel",
    "TenantMatterModel",
    "TenantMatterPartyModel",
]
