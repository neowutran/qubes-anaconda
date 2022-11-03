# DNF/rpm software payload management.
#
# Copyright (C) 2019  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
from pyanaconda.core.path import join_paths
from pyanaconda.modules.common.errors.installation import NonCriticalInstallationError, \
    InstallationError
from pyanaconda.modules.common.errors.payload import UnknownRepositoryError, SourceSetupError
from pyanaconda.modules.common.structures.payload import RepoConfigurationData
from pyanaconda.modules.common.structures.packages import PackagesConfigurationData, \
    PackagesSelectionData
from pyanaconda.modules.payloads.kickstart import convert_ks_repo_to_repo_data, \
    convert_repo_data_to_ks_repo
from pyanaconda.modules.payloads.payload.dnf.initialization import configure_dnf_logging
from pyanaconda.modules.payloads.payload.dnf.installation import ImportRPMKeysTask, \
    SetRPMMacrosTask, DownloadPackagesTask, InstallPackagesTask, PrepareDownloadLocationTask, \
    CleanUpDownloadLocationTask, ResolvePackagesTask, UpdateDNFConfigurationTask, \
    WriteRepositoriesTask
from pyanaconda.modules.payloads.payload.dnf.repositories import \
    generate_driver_disk_repositories, generate_treeinfo_repositories
from pyanaconda.modules.payloads.payload.dnf.tear_down import ResetDNFManagerTask
from pyanaconda.modules.payloads.payload.dnf.utils import get_kernel_version_list, \
    calculate_required_space
from pyanaconda.modules.payloads.payload.dnf.dnf_manager import DNFManager, DNFManagerError, \
    MetadataError
from pyanaconda.modules.payloads.source.harddrive.initialization import SetUpHardDriveSourceTask
from pyanaconda.modules.payloads.source.mount_tasks import TearDownMountTask
from pyanaconda.modules.payloads.source.nfs.initialization import SetUpNFSSourceTask
from pyanaconda.payload.source import SourceFactory, PayloadSourceTypeUnrecognized
from pyanaconda.anaconda_loggers import get_packaging_logger
from pyanaconda.core import constants
from pyanaconda.core.configuration.anaconda import conf
from pyanaconda.core.constants import INSTALL_TREE, ISO_DIR, PAYLOAD_TYPE_DNF, \
    SOURCE_TYPE_URL, SOURCE_TYPE_CDROM, URL_TYPE_BASEURL, SOURCE_REPO_FILE_TYPES, \
    SOURCE_TYPE_CDN, MULTILIB_POLICY_ALL, REPO_ORIGIN_SYSTEM, SOURCE_TYPE_CLOSEST_MIRROR
from pyanaconda.core.i18n import _
from pyanaconda.core.payload import parse_hdd_url
from pyanaconda.errors import errorHandler as error_handler, ERROR_RAISE
from pyanaconda.flags import flags
from pyanaconda.modules.common.constants.services import SUBSCRIPTION
from pyanaconda.modules.payloads.source.utils import has_network_protocol
from pyanaconda.modules.common.util import is_module_available
from pyanaconda.payload.base import Payload
from pyanaconda.payload.image import find_optical_install_media
from pyanaconda.modules.payloads.payload.dnf.tree_info import TreeInfoMetadata, NoTreeInfoError, \
    TreeInfoMetadataError
from pyanaconda.ui.lib.payload import get_payload, get_source, create_source, set_source, \
    set_up_sources, tear_down_sources

__all__ = ["DNFPayload"]

log = get_packaging_logger()


class DNFPayload(Payload):

    def __init__(self, data):
        super().__init__()
        self.data = data

        # A list of verbose error strings
        self.verbose_errors = []

        # Get a DBus payload to use.
        self._payload_proxy = get_payload(self.type)

        self.tx_id = None

        self._dnf_manager = DNFManager()

        # List of internal mount points.
        self._mount_points = []

        # Configure the DNF logging.
        configure_dnf_logging()

    @property
    def dnf_manager(self):
        """The DNF manager."""
        return self._dnf_manager

    @property
    def _base(self):
        """Return a DNF base.

        FIXME: This is a temporary property.
        """
        return self._dnf_manager._base

    def set_from_opts(self, opts):
        """Set the payload from the Anaconda cmdline options.

        :param opts: a namespace of options
        """
        self._set_source_from_opts(opts)
        self._set_source_configuration_from_opts(opts)
        self._set_additional_repos_from_opts(opts)
        self._generate_driver_disk_repositories()
        self._set_packages_from_opts(opts)

    def _set_source_from_opts(self, opts):
        """Change the source based on the Anaconda options.

        Set the source based on opts.method if it isn't already set
        - opts.method is currently set by command line/boot options.
        """
        if opts.method and (not self.proxy.Sources or self._is_source_default()):
            try:
                source = SourceFactory.parse_repo_cmdline_string(opts.method)
            except PayloadSourceTypeUnrecognized:
                log.error("Unknown method: %s", opts.method)
            else:
                source_proxy = source.create_proxy()
                set_source(self.proxy, source_proxy)

    def _set_source_configuration_from_opts(self, opts):
        """Configure the source based on the Anaconda options."""
        source_proxy = self.get_source_proxy()

        if source_proxy.Type == SOURCE_TYPE_URL:
            # Get the repo configuration.
            repo_configuration = RepoConfigurationData.from_structure(
                source_proxy.RepoConfiguration
            )

            if opts.proxy:
                repo_configuration.proxy = opts.proxy

            if not conf.payload.verify_ssl:
                repo_configuration.ssl_verification_enabled = conf.payload.verify_ssl

            # Update the repo configuration.
            source_proxy.RepoConfiguration = \
                RepoConfigurationData.to_structure(repo_configuration)

    def _set_additional_repos_from_opts(self, opts):
        """Set additional repositories based on the Anaconda options."""
        for repo_name, repo_url in opts.addRepo:
            try:
                source = SourceFactory.parse_repo_cmdline_string(repo_url)
            except PayloadSourceTypeUnrecognized:
                log.error("Type for additional repository %s is not recognized!", repo_url)
                return

            if self.get_addon_repo(repo_name):
                log.warning("Repository name %s is not unique. Only the first "
                            "repo will be used!", repo_name)

            is_supported = source.is_nfs \
                or source.is_http \
                or source.is_https \
                or source.is_ftp \
                or source.is_file \
                or source.is_harddrive

            if not is_supported:
                log.error("Source type %s for additional repository %s is not supported!",
                          source.source_type.value, repo_url)
                continue

            repo = RepoConfigurationData()
            repo.name = repo_name
            repo.enabled = True
            repo.type = URL_TYPE_BASEURL
            repo.url = repo_url
            repo.installation_enabled = False

            ks_repo = convert_repo_data_to_ks_repo(repo)
            self.data.repo.dataList().append(ks_repo)

    def _generate_driver_disk_repositories(self):
        """Append generated driver disk repositories."""
        for data in generate_driver_disk_repositories():
            ks_repo = convert_repo_data_to_ks_repo(data)
            self.data.repo.dataList().append(ks_repo)

    def _set_packages_from_opts(self, opts):
        """Configure packages based on the Anaconda options."""
        if opts.multiLib:
            configuration = self.get_packages_configuration()
            configuration.multilib_policy = MULTILIB_POLICY_ALL
            self.set_packages_configuration(configuration)

    @property
    def type(self):
        """The DBus type of the payload."""
        return PAYLOAD_TYPE_DNF

    def get_source_proxy(self):
        """Get the DBus proxy of the RPM source.

        The default source for the DNF payload is set via
        the default_source option in the payload section
        of the Anaconda config file.

        :return: a DBus proxy
        """
        return get_source(self.proxy)

    @property
    def source_type(self):
        """The DBus type of the source."""
        source_proxy = self.get_source_proxy()
        return source_proxy.Type

    def get_packages_configuration(self) -> PackagesConfigurationData:
        """Get the DBus data with the packages configuration."""
        return PackagesConfigurationData.from_structure(
            self.proxy.PackagesConfiguration
        )

    def set_packages_configuration(self, data: PackagesConfigurationData):
        """Set the DBus data with the packages configuration."""
        self.proxy.PackagesConfiguration = \
            PackagesConfigurationData.to_structure(data)

    def get_packages_selection(self) -> PackagesSelectionData:
        """Get the DBus data with the packages selection."""
        return PackagesSelectionData.from_structure(
            self.proxy.PackagesSelection
        )

    def set_packages_selection(self, data: PackagesSelectionData):
        """Set the DBus data with the packages selection."""
        self.proxy.PackagesSelection = \
            PackagesSelectionData.to_structure(data)

    def is_ready(self):
        """Is the payload ready?"""
        enabled_repos = self._dnf_manager.enabled_repositories

        # If CDN is used as the installation source and we have
        # a subscription attached then any of the enabled repos
        # should be fine as the base repo.
        # If CDN is used but subscription has not been attached
        # there will be no redhat.repo file to parse and we
        # don't need to do anything.
        if self.source_type == SOURCE_TYPE_CDN:
            return self._is_cdn_set_up() and enabled_repos

        # Otherwise, a base repository has to be enabled.
        return any(map(self._is_base_repo, enabled_repos))

    def _is_cdn_set_up(self):
        """Is the CDN source set up?"""
        if not self.source_type == SOURCE_TYPE_CDN:
            return False

        if not is_module_available(SUBSCRIPTION):
            return False

        subscription_proxy = SUBSCRIPTION.get_proxy()
        return subscription_proxy.IsSubscriptionAttached

    def _is_base_repo(self, repo_id):
        """Is it a base repository?"""
        return repo_id == constants.BASE_REPO_NAME \
            or repo_id in constants.DEFAULT_REPOS

    def is_complete(self):
        """Is the payload complete?"""
        return self.source_type not in SOURCE_REPO_FILE_TYPES or self.is_ready()

    def setup(self):
        self.verbose_errors = []

    def unsetup(self):
        self._dnf_manager.reset_base()
        tear_down_sources(self.proxy)

    @property
    def needs_network(self):
        """Test base and additional repositories if they require network."""
        return (self.service_proxy.IsNetworkRequired() or
                any(self._repo_needs_network(repo) for repo in self.data.repo.dataList()))

    def _repo_needs_network(self, repo):
        """Returns True if the ksdata repo requires networking."""
        urls = [repo.baseurl]
        if repo.mirrorlist:
            urls.extend(repo.mirrorlist)
        elif repo.metalink:
            urls.extend(repo.metalink)
        return self._source_needs_network(urls)

    def _source_needs_network(self, sources):
        """Return True if the source requires network.

        :param sources: Source paths for testing
        :type sources: list
        :returns: True if any source requires network
        """
        for s in sources:
            if has_network_protocol(s):
                log.debug("Source %s needs network for installation", s)
                return True

        log.debug("Source doesn't require network for installation")
        return False

    def bump_tx_id(self):
        if self.tx_id is None:
            self.tx_id = 1
        else:
            self.tx_id += 1
        return self.tx_id

    def _get_proxy_url(self):
        """Get a proxy of the current source.

        :return: a proxy or None
        """
        source_proxy = self.get_source_proxy()
        source_type = source_proxy.Type

        if source_type != SOURCE_TYPE_URL:
            return None

        data = RepoConfigurationData.from_structure(
            source_proxy.RepoConfiguration
        )

        return data.proxy

    ###
    # METHODS FOR WORKING WITH REPOSITORIES
    ###

    def get_addon_repo(self, repo_id):
        """Return a ksdata Repo instance matching the specified repo id."""
        repo = None
        for r in self.data.repo.dataList():
            if r.name == repo_id:
                repo = r
                break

        return repo

    def _handle_system_repository(self, data):
        """Handle a system repository.

        The user is trying to do "repo --name=updates" in a kickstart file.
        We can only enable or disable the already existing on-disk repo config.

        :raise: SourceSetupError if the system repository is not available
        """
        try:
            self._dnf_manager.set_repository_enabled(data.name, data.enabled)
        except UnknownRepositoryError:
            msg = "The '{}' repository is not one of the pre-defined repositories."
            raise SourceSetupError(msg.format(data.name)) from None

    def _set_up_additional_repository(self, data):
        """Set up sources for the additional repository."""
        # Check the validity of the repository.
        if not data.url:
            msg = "The '{}' repository has no mirror, baseurl or metalink set."
            raise SourceSetupError(msg.format(data.name)) from None

        # Set up the NFS source with a substituted URL.
        elif data.url.startswith("nfs://"):
            device_mount = self._create_mount_point(
                constants.MOUNT_DIR,
                data.name + "-nfs-device"
            )
            iso_mount = self._create_mount_point(
                constants.MOUNT_DIR,
                data.name + "-nfs-iso"
            )
            task = SetUpNFSSourceTask(
                device_mount=device_mount,
                iso_mount=iso_mount,
                url=self._dnf_manager.substitute(data.url)
            )
            mount_point = task.run()
            data.url = "file://" + mount_point

        # Set up the HDD source.
        elif data.url.startswith("hd:"):
            device_mount = self._create_mount_point(
                ISO_DIR + "-" + data.name + "-hdd-device"
            )
            iso_mount = self._create_mount_point(
                INSTALL_TREE + "-" + data.name + "-hdd-iso"
            )

            partition, directory = parse_hdd_url(data.url)

            task = SetUpHardDriveSourceTask(
                device_mount=device_mount,
                iso_mount=iso_mount,
                partition=partition,
                directory=directory,
            )
            result = task.run()
            data.url = "file://" + result.install_tree_path

    def _create_mount_point(self, *paths):
        """Create a mount point from specified paths.

        FIXME: This is a temporary workaround.
        """
        mount_point = join_paths(*paths)
        self._mount_points.append(mount_point)
        return mount_point

    def _tear_down_additional_sources(self):
        """Tear down sources of additional repositories.

        FIXME: This is a temporary workaround.
        """
        while self._mount_points:
            mount_point = self._mount_points.pop()
            task = TearDownMountTask(mount_point)
            task.run()

    @property
    def space_required(self):
        return calculate_required_space(self._dnf_manager)

    def install(self):
        self._progress_cb(0, _('Starting package installation process'))

        # Get the packages configuration and selection data.
        configuration = self.get_packages_configuration()
        selection = self.get_packages_selection()

        # Add the rpm macros to the global transaction environment
        task = SetRPMMacrosTask(configuration)
        task.run()

        try:
            # Resolve packages.
            task = ResolvePackagesTask(self._dnf_manager, selection)
            task.run()
        except NonCriticalInstallationError as e:
            # FIXME: This is a temporary workaround.
            # Allow users to handle the error. If they don't want
            # to continue with the installation, raise a different
            # exception to make sure that we will not run the error
            # handler again.
            if error_handler.cb(e) == ERROR_RAISE:
                raise InstallationError(str(e)) from e

        # Set up the download location.
        task = PrepareDownloadLocationTask(self._dnf_manager)
        task.run()

        # Download the packages.
        task = DownloadPackagesTask(self._dnf_manager)
        task.progress_changed_signal.connect(self._progress_cb)
        task.run()

        # Install the packages.
        task = InstallPackagesTask(self._dnf_manager)
        task.progress_changed_signal.connect(self._progress_cb)
        task.run()

        # Clean up the download location.
        task = CleanUpDownloadLocationTask(self._dnf_manager)
        task.run()

    def _is_source_default(self):
        """Report if the current source type is the default source type.

        NOTE: If no source was set previously a new default one
              will be created.
        """
        return self.source_type == conf.payload.default_source

    def update_base_repo(self, fallback=True, try_media=True):
        """Update the base repository from the DBus source."""
        log.debug("Tearing down sources")
        tear_down_sources(self.proxy)
        self._tear_down_additional_sources()

        log.debug("Preparing the DNF base")
        self.tx_id = None

        self._dnf_manager.clear_cache()
        self._dnf_manager.reset_substitution()
        self._dnf_manager.configure_base(self.get_packages_configuration())
        self._dnf_manager.configure_proxy(self._get_proxy_url())
        self._dnf_manager.dump_configuration()
        self._dnf_manager.read_system_repositories()

        log.info("Configuring the base repo")
        # Find the source and its type.
        source_proxy = self.get_source_proxy()
        source_type = source_proxy.Type

        # Change the default source to CDROM if there is a valid install media.
        # FIXME: Set up the default source earlier.
        if try_media and self._is_source_default() and find_optical_install_media():
            source_type = SOURCE_TYPE_CDROM
            source_proxy = create_source(source_type)
            set_source(self.proxy, source_proxy)

        # Set up the source.
        set_up_sources(self.proxy)

        # Add a new repo.
        if source_type not in SOURCE_REPO_FILE_TYPES:
            try:
                self._add_base_repository()
            except DNFManagerError as e:
                # Fail if the fallback is not enabled.
                if not fallback:
                    raise e

                # Fallback to the default source
                #
                # This is at the moment CDN on RHEL
                # and closest mirror everywhere else.
                tear_down_sources(self.proxy)

                source_type = conf.payload.default_source
                source_proxy = create_source(source_type)
                set_source(self.proxy, source_proxy)

                set_up_sources(self.proxy)

        # We need to check this again separately in case REPO_FILES were set above.
        if source_type in SOURCE_REPO_FILE_TYPES:
            # Remove all treeinfo repositories.
            self._remove_treeinfo_repositories()

            # If this is a kickstart install, just return now as we normally do not
            # want to read the on media repo files in such a case. On the other hand,
            # the local repo files are a valid use case if the system is subscribed
            # and the CDN is selected as the installation source.
            if flags.automatedInstall and not self._is_cdn_set_up():
                return

            # Otherwise, fall back to the default repos that we disabled above
            self._enable_system_repositories()

        self._include_additional_repositories()
        self._validate_enabled_repositories()

    def _add_base_repository(self):
        """Add the base repository.

        Try to add a valid base repository to the DNF base.

        :raise: DNFManagerError if the repo is invalid
        """
        # Get the repo configuration of the first source.
        data = RepoConfigurationData.from_structure(
            self.proxy.GetRepoConfigurations()[0]
        )

        log.debug("Using the repo configuration: %s", data)
        data.name = constants.BASE_REPO_NAME

        # Process the treeinfo metadata.
        treeinfo_base_repo_url = self._reload_treeinfo_metadata(data)

        if treeinfo_base_repo_url:
            data.type = URL_TYPE_BASEURL
            data.url = treeinfo_base_repo_url

        log.debug("Add the base repository at %s.", data.url)

        try:
            self._dnf_manager.add_repository(data)
            self._dnf_manager.load_repository(data.name)
        except DNFManagerError as e:
            log.error("The base repository is invalid: %s", str(e))
            self._dnf_manager.remove_repository(data.name)
            raise e

    def _enable_system_repositories(self):
        """Enable system repositories.

        * Restore previously disabled system repositories.
        * Enable or disable system repositories based on the current configuration.
        """
        self._dnf_manager.restore_system_repositories()

        log.debug("Enable or disable updates repositories.")
        updates_enabled = self._get_updates_enabled()
        self._set_repositories_enabled(conf.payload.updates_repositories, updates_enabled)

        log.debug("Disable repositories based on the Anaconda configuration file.")
        self._set_repositories_enabled(conf.payload.disabled_repositories, False)

        if constants.isFinal:
            log.debug("Disable rawhide repositories.")
            self._set_repositories_enabled(["*rawhide*"], False)

    def _get_updates_enabled(self):
        """Are latest updates enabled?"""
        source_proxy = self.get_source_proxy()
        source_type = source_proxy.Type

        if source_type == SOURCE_TYPE_CLOSEST_MIRROR:
            return source_proxy.UpdatesEnabled
        else:
            return False

    def _set_repositories_enabled(self, patterns, enabled):
        """Enable or disable matching repositories.

        :param patterns: a list of patterns to match the repo ids
        :param enabled: True to enable, False to disable
        """
        repo_ids = set()

        for pattern in patterns:
            repo_ids.update(self._dnf_manager.get_matching_repositories(pattern))

        for repo_id in sorted(repo_ids):
            self.dnf_manager.set_repository_enabled(repo_id, enabled)

    def _include_additional_repositories(self):
        """Add additional repositories to DNF."""
        for ks_repo in self.data.repo.dataList():
            # Convert the repository.
            data = convert_ks_repo_to_repo_data(ks_repo)
            log.debug("Add the '%s' repository (%s).", data.name, data)

            # A system repository can be only enabled or disabled.
            if data.origin == REPO_ORIGIN_SYSTEM:
                self._handle_system_repository(data)
                return

            # Set up additional sources.
            self._set_up_additional_repository(data)

            # Add a new repository.
            self._dnf_manager.add_repository(data)

            # Load an enabled repository to check its validity.
            self._dnf_manager.load_repository(data.name)

    def _validate_enabled_repositories(self):
        """Validate all enabled repositories.

        Collect error messages about invalid repositories.
        All invalid repositories are disabled.

        The user repositories are validated when we add them
        to DNF, so this covers invalid system repositories.
        """
        for repo_id in self.dnf_manager.enabled_repositories:
            try:
                self.dnf_manager.load_repository(repo_id)
            except MetadataError as e:
                self.verbose_errors.append(str(e))

    def _reload_treeinfo_metadata(self, repo_data):
        """Reload treeinfo metadata.

        :param RepoConfigurationData repo_data: configuration data of the base repo
        :return: a URL of the base repository
        """
        log.debug("Reload treeinfo metadata.")
        base_repo_url = None

        # Remove previous treeinfo repositories.
        removed_repos = self._remove_treeinfo_repositories()
        disabled_names = [r.name for r in removed_repos if not r.enabled]

        # Collect URLs of existing repositories.
        existing_urls = [r.baseurl for r in self.data.repo.dataList() if r.baseurl]

        try:
            # Load the treeinfo metadata.
            tree_info_metadata = TreeInfoMetadata()
            tree_info_metadata.load_data(repo_data)

            # Set up the substitutions.
            self._dnf_manager.configure_substitution(
                tree_info_metadata.release_version
            )

            # Get the new base repo URL.
            base_repo_url = tree_info_metadata.get_base_repo_url()
            existing_urls.append(base_repo_url)

            # Add the treeinfo repositories.
            repositories = generate_treeinfo_repositories(
                repo_data,
                tree_info_metadata
            )

            self._add_treeinfo_repositories(
                repositories=repositories,
                disabled_names=disabled_names,
                existing_urls=existing_urls,
            )
        except NoTreeInfoError as e:
            log.debug("No treeinfo metadata to use: %s", str(e))
        except TreeInfoMetadataError as e:
            log.warning("Couldn't use treeinfo metadata: %s", str(e))

        return base_repo_url

    def _add_treeinfo_repositories(self, repositories, disabled_names, existing_urls):
        """Add the treeinfo repositories.

        :param [RepoConfigurationData] repositories: configuration data of treeinfo repositories
        :param [str] disabled_names: a list of repository names that should be disabled
        :param [str] existing_urls: a list of repository URLs that already exist
        """
        log.debug("Add treeinfo repositories.")

        for repo in repositories:
            # Skip existing repositories.
            if repo.url in existing_urls:
                continue

            # Disable if previously disabled.
            if repo.name in disabled_names:
                repo.enabled = False

            log.debug("Add the '%s' treeinfo repository: %s", repo.name, repo)

            # Add the repository to user repositories,
            # so it'll appear in the output ks file.
            ks_repo = convert_repo_data_to_ks_repo(repo)
            self.data.repo.dataList().append(ks_repo)

    def _remove_treeinfo_repositories(self):
        """Remove all old treeinfo repositories before loading new ones.

        Find all repositories added from treeinfo file and remove them.
        After this step new repositories will be loaded from the new link.

        :return: list of repositories that were removed
        """
        log.debug("Remove treeinfo repositories.")
        removed_repos = []

        for ks_repo in list(self.data.repo.dataList()):
            if not ks_repo.treeinfo_origin:
                continue

            self.data.repo.dataList().remove(ks_repo)
            removed_repos.append(ks_repo)

            log.debug("Removed the '%s' treeinfo repository.", ks_repo.name)

        return removed_repos

    def post_install(self):
        """Perform post-installation tasks."""
        super().post_install()

        # Write selected kickstart repos to target system
        repositories = list(map(
            convert_ks_repo_to_repo_data,
            self.data.repo.dataList()
        ))

        task = WriteRepositoriesTask(
            sysroot=conf.target.system_root,
            dnf_manager=self.dnf_manager,
            repositories=repositories,
        )
        task.run()

        # rpm needs importing installed certificates manually, see rhbz#748320 and rhbz#185800
        task = ImportRPMKeysTask(
            sysroot=conf.target.system_root,
            gpg_keys=conf.payload.default_rpm_gpg_keys
        )
        task.run()

        # Update the DNF configuration.
        task = UpdateDNFConfigurationTask(
            sysroot=conf.target.system_root,
            data=self.get_packages_configuration()
        )
        task.run()

        # Close the DNF base.
        task = ResetDNFManagerTask(
            dnf_manager=self.dnf_manager
        )
        task.run()

    @property
    def kernel_version_list(self):
        return get_kernel_version_list()
