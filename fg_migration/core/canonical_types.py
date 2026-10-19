""" Note: I created these types to make it explict what fields are currently being handled by the
          migration.
          In theory we could either use the Forgejo types directly, or extend them e.g.
          class CanonicalUser(pyforgejo.User) BUT, the issue is that you'll lose the ability to
          see what is and isn't happening as it won't be explicit. Possibly I might change this
          to extend the core types, if we get most fields migrated, but, it helps to know
          where an object has come from and that is obvious when seeing the type is
          Canonical<ForgejoTypeName>
          These types have fields such as username, they are the Source System username.
          the get_safe_username function is to convert that to Forgejo equivalent"""
from dataclasses import dataclass, field
from enum import StrEnum



from fg_migration.utils.utils import name_clean




@dataclass
class CanonicalUser:
    """Typesafe definition of a User in its most basic form"""
    username: str

    def get_safe_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.username)



@dataclass
class CanonicalGpgKey:
    """A GPG Key"""
    name:str
    armored_public_key: str
    armored_signature:str|None



@dataclass
class CanonicalKey:
    """An SSH Key"""
    name:str
    key: str



@dataclass
class CanonicalSystemUser:
    """A System User (i.e. that can login)"""
    source_system:str # e.g. GitLab, GitHub
    username: str
    full_name: str
    email: str
    avatar_url:str|None
    # The password will be set to a new temporary one when creating the
    # user but should otherwise be None
    password:str|None
    gpg_keys:list[CanonicalGpgKey] = field(default_factory=list)
    keys:list[CanonicalKey] = field(default_factory=list)

    def get_safe_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.username)



@dataclass
class CanonicalOrganizations:
    """Group of Owners of repositories and teams. Really this just allows definition
       of the source type which is typically a plural of the CanonicalOrganization
       source type (used in logging)"""
    source_type:str # what this type is called at the source, e.g. GitLab Groups
    members:list["CanonicalOrganization"] = field(default_factory=list)



@dataclass
class CanonicalOrganizationMembership:
    """A mapping from Organization to user that is a member"""
    org_username: str
    username: str
    access_level: str

    def get_safe_org_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.org_username)

    def get_safe_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.username)


@dataclass
class CanonicalOrganization:
    """Owner of repositories and teams"""
    source_system:str
    source_type:str # what this type is called at the source, e.g. GitLab Group
    username: str
    full_name:str
    description:str
    members: list[CanonicalUser] = field(default_factory=list)
    memberships: list[CanonicalOrganizationMembership] = field(default_factory=list)

    def get_safe_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.username)



@dataclass
class CanonicalRepoOwner:
    """Owner of a repository (either user identifier or organization identifier)"""
    id:int|None
    username:str|None

    def is_complete(self) -> bool:
        """Is the repo owner fully defined (theoretically this should always be true)"""
        return self.id is not None and self.username is not None


# frozen permits its use in sets, and I can think of no good reason to ever alter the contents.
@dataclass(frozen=True)
class CanonicalRepoMembership:
    """Mapping between repository and user with access including the permission they have there.
       Hierarchy can be used to store path in the source system to the member, for example
       in GitLab it might be set for a descendant group to <group>/:d:/<group><group>
       or for a sub group to <group>/:s:/<group>
       This could then be parsed when creating teams to build compound team names such as
       org/team-d-descendant-descendant or org/team-s-sub for example, thus flattening a deep
       nested team / group structure in the source system."""
    username:str
    repository:"CanonicalRepo"
    hierarchy:list["HierarchyNode"] | None
    access_level:str

    def get_safe_username(self) -> str:
        """Get Forgejo safe username"""
        return name_clean(self.username)

@dataclass(frozen=True)
class HierarchyNode:
    """Define the Source system hierachy of some Canonical Object as
       explained in the class CanonicalRepoMembership"""
    name: str
    relation: "RelationEnum|None" = None

    class RelationEnum(StrEnum):
        "Type of hierarchy node"
        SUB = "sub"
        DESCENDANT = "desc"



@dataclass
class CanonicalRepoMemberships:
    """This is essentially a wrapper around the list of CanonicalRepoMembership
       allowing the plural to be recorded"""
    source_system:str # e.g. GitLab, GitHub
    members:list[CanonicalRepoMembership] = field(default_factory=list)
    source_type:str = "Users" # what this type is called at the source

    @staticmethod
    def get_grouped_by_access_level(members:list[CanonicalRepoMembership]
                                         ) -> dict[str,set[CanonicalRepoMembership]]:
        """Builds a list of memberships that have been grouped by their access level"""
        grouped_by_access_level : dict[str,set[CanonicalRepoMembership]] = {}
        for member in members:
            grouped_by_access_level.get(member.access_level, set()).add(member)
        return grouped_by_access_level



@dataclass
class CanonicalRepo:
    """A repository for code"""
    is_individual:bool # Owned by a user? (else an organization)
    username:str
    # Note, in some systems, a user friendly name may be possible,
    # but in Forgejo, there is just the username at present.
    name:str
    description:str
    owner_name: str
    clone_url:str
    is_private:bool
    auth_password:str
    auth_username:str
    auth_token:str
    source_system:str # e.g. GitLab, GitHub
    # source_id - the id of this object in the source system
    source_id:str
    # source_type - what this type is called at the source, e.g. GitLab Project
    source_type: str = "Repository"
    # archived (read only) in the source system, so it should be archived in Forgejo too
    is_archived: bool = False

    def get_safe_username(self) -> str:
        """Get Forgejo safe username: the source path (e.g. my-project), not the display
           name (My Project), so urls stay the same"""
        return name_clean(self.username)

    def get_safe_owner_name(self) -> str:
        """Get Forgejo safe owner name"""
        return name_clean(self.owner_name)
