# -*- coding: utf-8 -*-
"""The extractor class definitions.

An extractor is a class used to extract information from "raw" data.
"""

import copy
import hashlib
import logging

import pysigscan

from dfvfs.helpers import file_system_searcher
from dfvfs.lib import definitions as dfvfs_definitions
from dfvfs.lib import errors as dfvfs_errors
from dfvfs.resolver import resolver as path_spec_resolver

from plaso.lib import errors
from plaso.parsers import interface as parsers_interface
from plaso.parsers import manager as parsers_manager


class EventExtractor(object):
  """Class that implements an event extractor object.

  The event extractor extracts events from event sources.
  """

  def __init__(self, resolver_context, parser_filter_expression=None):
    """Initializes an event extractor object.

    Args:
      resolver_context (dfvfs.Context): resolver context.
      parser_filter_expression (Optional[str]): the parser filter expression.
          None represents all parsers and plugins.

          The parser filter expression is a comma separated value string that
          denotes a list of parser names to include and/or exclude. Each entry
          can have the value of:

          * An exact match of a list of parsers, or a preset (see
            plaso/frontend/presets.py for a full list of available presets).
          * A name of a single parser (case insensitive), e.g. msiecf.
          * A glob name for a single parser, e.g. '*msie*' (case insensitive).
    """
    super(EventExtractor, self).__init__()
    self._file_scanner = None
    self._filestat_parser = None
    self._mft_parser = None
    self._non_sigscan_parser_names = None
    self._parser_filter_expression = parser_filter_expression
    self._parsers = None
    self._parsers_profiler = None
    self._resolver_context = resolver_context
    self._specification_store = None
    self._usnjrnl_parser = None

    self._InitializeParserObjects()

  def _CheckParserCanProcessFileEntry(self, parser, file_entry):
    """Determines if a parser can process a file entry.

    Args:
      file_entry (dfvfs.FileEntry): file entry.
      parser (BaseParser): parser.

    Returns:
      bool: True if the file entry can be processed by the parser object.
    """
    for filter_object in parser.FILTERS:
      if filter_object.Match(file_entry):
        return True

    return False

  def _GetSignatureMatchParserNames(self, file_object):
    """Determines if a file-like object matches one of the known signatures.

    Args:
      file_object (file):
          file-like object whose contents will be checked for known signatures.

    Returns:
      list[str]: parser names for which the contents of the file-like object
                 matches their known signatures.
    """
    parser_name_list = []
    scan_state = pysigscan.scan_state()
    self._file_scanner.scan_file_object(scan_state, file_object)

    for scan_result in iter(scan_state.scan_results):
      format_specification = (
          self._specification_store.GetSpecificationBySignature(
              scan_result.identifier))

      if format_specification.identifier not in parser_name_list:
        parser_name_list.append(format_specification.identifier)

    return parser_name_list

  def _InitializeParserObjects(self):
    """Initializes the parser objects."""
    self._specification_store, non_sigscan_parser_names = (
        parsers_manager.ParsersManager.GetSpecificationStore(
            parser_filter_expression=self._parser_filter_expression))

    self._non_sigscan_parser_names = []
    for parser_name in non_sigscan_parser_names:
      if parser_name in (u'filestat', u'usnjrnl'):
        continue
      self._non_sigscan_parser_names.append(parser_name)

    self._file_scanner = parsers_manager.ParsersManager.GetScanner(
        self._specification_store)

    self._parsers = parsers_manager.ParsersManager.GetParserObjects(
        parser_filter_expression=self._parser_filter_expression)

    self._filestat_parser = self._parsers.get(u'filestat', None)
    if u'filestat' in self._parsers:
      del self._parsers[u'filestat']

    self._mft_parser = self._parsers.get(u'mft', None)

    self._usnjrnl_parser = self._parsers.get(u'usnjrnl', None)
    if u'usnjrnl' in self._parsers:
      del self._parsers[u'usnjrnl']

  def _ParseDataStreamWithParser(
      self, parser_mediator, parser, file_entry, data_stream_name):
    """Parses a data stream of a file entry with a specific parser.

    Args:
      parser_mediator (ParserMediator): parser mediator.
      parser (BaseParser): parser.
      file_entry (dfvfs.FileEntry): file entry.
      data_stream_name (str): data stream name.

    Raises:
      RuntimeError: if the file-like object is missing.
    """
    file_object = file_entry.GetFileObject(data_stream_name=data_stream_name)
    if not file_object:
      raise RuntimeError(
          u'Unable to retrieve file-like object from file entry.')

    try:
      self._ParseFileEntryWithParser(
          parser_mediator, parser, file_entry, file_object=file_object)

    finally:
      file_object.close()

  def _ParseFileEntryWithParser(
      self, parser_mediator, parser, file_entry, file_object=None):
    """Parses a file entry with a specific parser.

    Args:
      parser_mediator (ParserMediator): parser mediator.
      parser (BaseParser): parser.
      file_entry (dfvfs.FileEntry): file entry.
      file_object (Optional[file]): file-like object to parse.
          If not set the parser will use the parser mediator to open
          the file entry's default data stream as a file-like object.

    Raises:
      TypeError: if parser object is not a supported parser type.
    """
    if not isinstance(parser, (
        parsers_interface.FileEntryParser, parsers_interface.FileObjectParser)):
      raise TypeError(u'Unsupported parser object type.')

    parser_mediator.ClearParserChain()

    reference_count = self._resolver_context.GetFileObjectReferenceCount(
        file_entry.path_spec)

    if self._parsers_profiler:
      self._parsers_profiler.StartTiming(parser.NAME)

    try:
      if isinstance(parser, parsers_interface.FileEntryParser):
        parser.Parse(parser_mediator)
      elif isinstance(parser, parsers_interface.FileObjectParser):
        parser.Parse(parser_mediator, file_object)

    # We catch IOError so we can determine the parser that generated the error.
    except (IOError, dfvfs_errors.BackEndError) as exception:
      display_name = parser_mediator.GetDisplayName(file_entry)
      logging.warning(
          u'{0:s} unable to parse file: {1:s} with error: {2:s}'.format(
              parser.NAME, display_name, exception))

    except errors.UnableToParseFile as exception:
      display_name = parser_mediator.GetDisplayName(file_entry)
      logging.debug(
          u'{0:s} unable to parse file: {1:s} with error: {2:s}'.format(
              parser.NAME, display_name, exception))

    finally:
      if self._parsers_profiler:
        self._parsers_profiler.StopTiming(parser.NAME)

      if reference_count != self._resolver_context.GetFileObjectReferenceCount(
          file_entry.path_spec):
        display_name = parser_mediator.GetDisplayName(file_entry)
        logging.warning((
            u'[{0:s}] did not explicitly close file-object for file: '
            u'{1:s}.').format(parser.NAME, display_name))

  def ParseDataStream(self, parser_mediator, file_entry, data_stream_name):
    """Parses a data stream of a file entry with the enabled parsers.

    Args:
      parser_mediator (ParserMediator): parser mediator.
      file_entry (dfvfs.FileEntry): file entry.
      data_stream_name (str): data stream name.

    Raises:
      RuntimeError: if the file-like object or the parser object is missing.
    """
    file_object = file_entry.GetFileObject(data_stream_name=data_stream_name)
    if not file_object:
      raise RuntimeError(
          u'Unable to retrieve file-like object from file entry.')

    try:
      parser_name_list = self._GetSignatureMatchParserNames(file_object)
      if not parser_name_list:
        parser_name_list = self._non_sigscan_parser_names

      for parser_name in parser_name_list:
        parser = self._parsers.get(parser_name, None)
        if not parser:
          raise RuntimeError(
              u'Parser object missing for parser: {0:s}'.format(parser_name))

        if parser.FILTERS:
          if not self._CheckParserCanProcessFileEntry(
              parser, file_entry):
            continue

        display_name = parser_mediator.GetDisplayName(file_entry)
        logging.debug((
            u'[ParseDataStream] parsing file: {0:s} with parser: '
            u'{1:s}').format(display_name, parser_name))

        self._ParseFileEntryWithParser(
            parser_mediator, parser, file_entry, file_object=file_object)

    finally:
      file_object.close()

  def ParseFileEntryMetadata(self, parser_mediator, file_entry):
    """Parses the file entry metadata e.g. file system data.

    Args:
      parser_mediator (ParserMediator): parser mediator.
      file_entry (dfvfs.FileEntry): file entry.
    """
    if self._filestat_parser:
      self._ParseFileEntryWithParser(
          parser_mediator, self._filestat_parser, file_entry)

  def ParseMetadataFile(
      self, parser_mediator, file_entry, data_stream_name):
    """Parses a metadata file.

    Args:
      parser_mediator (ParserMediator): parser mediator.
      file_entry (dfvfs.FileEntry): file entry.
      data_stream_name (str): data stream name.
    """
    parent_path_spec = getattr(file_entry.path_spec, u'parent', None)
    filename_upper = file_entry.name.upper()
    if (self._mft_parser and parent_path_spec and
        filename_upper in (u'$MFT', u'$MFTMIRR') and not data_stream_name):
      self._ParseDataStreamWithParser(
          parser_mediator, self._mft_parser, file_entry, u'')

    elif (self._usnjrnl_parser and parent_path_spec and
          filename_upper == u'$USNJRNL' and data_stream_name == u'$J'):
      # To be able to ignore the sparse data ranges the UsnJrnl parser
      # needs to read directly from the volume.
      volume_file_object = path_spec_resolver.Resolver.OpenFileObject(
          parent_path_spec, resolver_context=self._resolver_context)

      try:
        self._ParseFileEntryWithParser(
            parser_mediator, self._usnjrnl_parser, file_entry,
            file_object=volume_file_object)
      finally:
        volume_file_object.close()

  def SetParsersProfiler(self, parsers_profiler):
    """Sets the parsers profiler.

    Args:
      parsers_profiler (ParsersProfiler): parsers profile.
    """
    self._parsers_profiler = parsers_profiler


class PathSpecExtractor(object):
  """Class that implements a path specification extractor object.

  The path specification extractor extracts path specification from a source
  directory, file or storage media device or image.
  """

  _MAXIMUM_DEPTH = 255

  def __init__(self, resolver_context, duplicate_file_check=False):
    """Initializes a path specification extractor object.

    The source collector discovers all the file entries in the source.
    The source can be a single file, directory or a volume within
    a storage media image or device.

    Args:
      resolver_context (dfvfs.Context): resolver context.
      duplicate_file_check (Optional[bool]):
          True if duplicate files should be ignored.
    """
    super(PathSpecExtractor, self).__init__()
    self._duplicate_file_check = duplicate_file_check
    self._hashlist = {}
    self._resolver_context = resolver_context

  def _CalculateNTFSTimeHash(self, file_entry):
    """Returns a hash value calculated from a NTFS file entry.

    Args:
      file_entry (dfvfs.FileEntry): file entry.

    Returns:
      str: hash value of the file entry.
    """
    stat_object = file_entry.GetStat()
    ret_hash = hashlib.md5()

    atime = getattr(stat_object, u'atime', None)
    if not atime:
      atime = 0
    ret_hash.update(b'atime:{0:d}.{1:d}'.format(
        atime, getattr(stat_object, u'atime_nano', 0)))

    crtime = getattr(stat_object, u'crtime', None)
    if not crtime:
      crtime = 0
    ret_hash.update(b'crtime:{0:d}.{1:d}'.format(
        crtime, getattr(stat_object, u'crtime_nano', 0)))

    mtime = getattr(stat_object, u'mtime', None)
    if not mtime:
      mtime = 0
    ret_hash.update(b'mtime:{0:d}.{1:d}'.format(
        mtime, getattr(stat_object, u'mtime_nano', 0)))

    ctime = getattr(stat_object, u'ctime', None)
    if not ctime:
      ctime = 0
    ret_hash.update(b'ctime:{0:d}.{1:d}'.format(
        ctime, getattr(stat_object, u'ctime_nano', 0)))

    return ret_hash.hexdigest()

  def _ExtractPathSpecs(
      self, path_spec, find_specs=None, recurse_file_system=True):
    """Extracts path specification from a specific source.

    Args:
      path_spec (dfvfs.PathSpec): path specification.
      find_specs (Optional[list[dfvfs.FindSpec]]): find specifications
          used in path specification extraction.
      recurse_file_system (Optional[bool]): True if extraction should
          recurse into a file system.

    Yields:
      dfvfs.PathSpec: path specification of a file entry found in the source.
    """
    try:
      file_entry = path_spec_resolver.Resolver.OpenFileEntry(
          path_spec, resolver_context=self._resolver_context)
    except (
        dfvfs_errors.AccessError, dfvfs_errors.BackEndError,
        dfvfs_errors.PathSpecError) as exception:
      logging.error(
          u'Unable to open file entry with error: {0:s}'.format(exception))
      return

    if not file_entry:
      logging.warning(u'Unable to open: {0:s}'.format(path_spec.comparable))
      return

    if (not file_entry.IsDirectory() and not file_entry.IsFile() and
        not file_entry.IsDevice()):
      logging.warning((
          u'Source path specification not a device, file or directory.\n'
          u'{0:s}').format(path_spec.comparable))
      return

    if file_entry.IsFile():
      yield path_spec

    else:
      for path_spec in self._ExtractPathSpecsFromFileSystem(
          path_spec, find_specs=find_specs,
          recurse_file_system=recurse_file_system):
        yield path_spec

  def _ExtractPathSpecsFromDirectory(self, file_entry, depth=0):
    """Extracts path specification from a directory.

    Args:
      file_entry (dfvfs.FileEntry): file entry that refers to the directory.
      depth (Optional[int]): current depth where 0 represents the file system
          root.

    Yields:
      dfvfs.PathSpec: path specification of a file entry found in the directory.
    """
    if depth >= self._MAXIMUM_DEPTH:
      raise errors.MaximumRecursionDepth(u'Maximum recursion depth reached.')

    # Need to do a breadth-first search otherwise we'll hit the Python
    # maximum recursion depth.
    sub_directories = []

    for sub_file_entry in file_entry.sub_file_entries:
      try:
        if not sub_file_entry.IsAllocated() or sub_file_entry.IsLink():
          continue
      except dfvfs_errors.BackEndError as exception:
        logging.warning(
            u'Unable to process file: {0:s} with error: {1:s}'.format(
                sub_file_entry.path_spec.comparable.replace(
                    u'\n', u';'), exception))
        continue

      # For TSK-based file entries only, ignore the virtual /$OrphanFiles
      # directory.
      if sub_file_entry.type_indicator == dfvfs_definitions.TYPE_INDICATOR_TSK:
        if file_entry.IsRoot() and sub_file_entry.name == u'$OrphanFiles':
          continue

      if sub_file_entry.IsDirectory():
        sub_directories.append(sub_file_entry)

      elif sub_file_entry.IsFile():
        # If we are dealing with a VSS we want to calculate a hash
        # value based on available timestamps and compare that to previously
        # calculated hash values, and only include the file into the queue if
        # the hash does not match.
        if self._duplicate_file_check:
          hash_value = self._CalculateNTFSTimeHash(sub_file_entry)

          inode = getattr(sub_file_entry.path_spec, u'inode', 0)
          if inode in self._hashlist:
            if hash_value in self._hashlist[inode]:
              continue

          self._hashlist.setdefault(inode, []).append(hash_value)

      for path_spec in self._ExtractPathSpecsFromFile(sub_file_entry):
        yield path_spec

    for sub_file_entry in sub_directories:
      try:
        for path_spec in self._ExtractPathSpecsFromDirectory(
            sub_file_entry, depth=(depth + 1)):
          yield path_spec

      except (
          dfvfs_errors.AccessError, dfvfs_errors.BackEndError,
          dfvfs_errors.PathSpecError) as exception:
        logging.warning(u'{0:s}'.format(exception))

  def _ExtractPathSpecsFromFile(self, file_entry):
    """Extracts path specification from a file.

    Args:
      file_entry (dfvfs.FileEntry): file entry that refers to the file.

    Yields:
      dfvfs.PathSpec: path specification of a file entry found in the file.
    """
    produced_main_path_spec = False
    for data_stream in file_entry.data_streams:
      # Make a copy so we don't make the changes on a path specification
      # directly. Otherwise already produced path specifications can be
      # altered in the process.
      path_spec = copy.deepcopy(file_entry.path_spec)
      if data_stream.name:
        setattr(path_spec, u'data_stream', data_stream.name)
      yield path_spec

      if not data_stream.name:
        produced_main_path_spec = True

    if not produced_main_path_spec:
      yield file_entry.path_spec

  def _ExtractPathSpecsFromFileSystem(
      self, path_spec, find_specs=None, recurse_file_system=True):
    """Extracts path specification from a file system within a specific source.

    Args:
      path_spec (dfvfs.PathSpec): path specification of the root of
                                  the file system.
      find_specs (Optional[list[dfvfs.FindSpec]]): find specifications.
      recurse_file_system (Optional[bool]):
          True if extraction should recurse into a file system.

    Yields:
      dfvfs.PathSpec: path specification of a file entry found in
                      the file system.
    """
    try:
      file_system = path_spec_resolver.Resolver.OpenFileSystem(
          path_spec, resolver_context=self._resolver_context)
    except (
        dfvfs_errors.AccessError, dfvfs_errors.BackEndError,
        dfvfs_errors.PathSpecError) as exception:
      logging.error(
          u'Unable to open file system with error: {0:s}'.format(exception))
      return

    try:
      if find_specs:
        searcher = file_system_searcher.FileSystemSearcher(
            file_system, path_spec)
        for path_spec in searcher.Find(find_specs=find_specs):
          yield path_spec

      elif recurse_file_system:
        file_entry = file_system.GetFileEntryByPathSpec(path_spec)
        if file_entry:
          for path_spec in self._ExtractPathSpecsFromDirectory(file_entry):
            yield path_spec

      else:
        yield path_spec

    except (
        dfvfs_errors.AccessError, dfvfs_errors.BackEndError,
        dfvfs_errors.PathSpecError) as exception:
      logging.warning(u'{0:s}'.format(exception))

    finally:
      file_system.Close()

  def ExtractPathSpecs(
      self, path_specs, find_specs=None, recurse_file_system=True):
    """Extracts path specification from a specific source.

    Args:
      path_specs (Optional[list[dfvfs.PathSpec]]): path specifications.
      find_specs (Optional[list[dfvfs.FindSpec]]): find specifications.
      recurse_file_system (Optional[bool]):
          True if extraction should recurse into a file system.

    Yields:
      dfvfs.PathSpec: path specification of a file entry found in the source.
    """
    for path_spec in path_specs:
      for extracted_path_spec in self._ExtractPathSpecs(
          path_spec, find_specs=find_specs,
          recurse_file_system=recurse_file_system):
        yield extracted_path_spec