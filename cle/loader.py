#!/usr/bin/env python

import os
import logging
import shutil
import subprocess
import struct
from collections import OrderedDict

from .errors import CLEError, CLEOperationError, CLEFileNotFoundError, CLECompatibilityError
from .memory import Clemory
from .tls import TLSObj

__all__ = ('Loader',)

l = logging.getLogger("cle.loader")

class Loader(object):
    """ CLE ELF loader
    The loader loads all the objects and exports an abstraction of the memory of
    the process. What you see here is an address space with loaded and rebased
    binaries.

    Class variables:
       memory             The loaded, rebased, and relocated memory of the program
       main_bin           The object representing the main binary (i.e., the executable)
       shared_objects     A dictionary mapping loaded library names to the objects representing them
       all_objects        A list containing representations of all the different objects loaded
       requested_objects  A set containing the names of all the different shared libraries that were marked as a dependancy by somebody
       tls_object         An object dealing with the region of memory allocated for thread-local storage

    When reference is made to a dictionary of options, it require a dictionary with zero or more of the following keys:
        backend             "elf", "pe", "ida", "blob": which loader backend to use
        custom_arch         The archinfo.Arch object to use for the binary
        custom_base_addr    The address to rebase the object at
        custom_entry_point  The entry point to use for the object
        ???                 More, defined on a per-backend basis
    """

    def __init__(self, main_binary, auto_load_libs=True,
                 force_load_libs=None, skip_libs=None,
                 main_opts=None, lib_opts=None, custom_ld_path=None,
                 ignore_import_version_numbers=True, rebase_granularity=0x1000000,
                 except_missing_libs=False):
        """
        @param main_binary      The path to the main binary you're loading
        @param auto_load_libs   Whether to automatically load shared libraries that
                                loaded objects depend on
        @param force_load_libs  A list of libraries to load regardless of if they're
                                required by a loaded object
        @param skip_libs        A list of libraries to never load, even if they're
                                required by a loaded object
        @param main_opts        A dictionary of options to be used loading the
                                main binary
        @param lib_opts         A dictionary mapping library names to the dictionaries
                                of options to be used when loading them
        @param custom_ld_path   A list of paths in which we can search for shared libraries
        @param ignore_import_version_numbers
                                Whether libraries with different version numbers in the
                                filename will be considered equivilant, for example
                                libc.so.6 and libc.so.0
        @param rebase_granularity
                                The alignment to use for rebasing shared objects
        @param except_missing_libs
                                Throw an exception when a shared library can't be found
        """

        self._main_binary_path = os.path.realpath(str(main_binary))
        self._auto_load_libs = auto_load_libs
        self._unsatisfied_deps = [] if force_load_libs is None else force_load_libs
        self._satisfied_deps = set([] if skip_libs is None else skip_libs)
        self._main_opts = {} if main_opts is None else main_opts
        self._lib_opts = {} if lib_opts is None else lib_opts
        self._custom_ld_path = [] if custom_ld_path is None else custom_ld_path
        self._ignore_import_version_numbers = ignore_import_version_numbers
        self._rebase_granularity = rebase_granularity
        self._except_missing_libs = except_missing_libs
        self._relocated_objects = set()

        self.memory = None
        self.main_bin = None
        self.shared_objects = {}
        self.all_objects = []
        self.requested_objects = set()
        self.tls_object = None

        self._load_main_binary()
        self._load_dependencies()
        self._load_tls()
        self._perform_reloc(self.main_bin)
        self._finalize_tls()

    def __repr__(self):
        return '<Loaded %s, maps [%#x:%#x]>' % (os.path.basename(self._main_binary_path), self.min_addr(), self.max_addr())

    def get_initializers(self):
        '''
         Return a list of all the initializers that should be run before execution reaches
         the entry point, in the order they should be run.
        '''
        return sum(map(lambda x: x.get_initializers(), self.all_objects), [])

    def get_finalizers(self):
        '''
         Return a list of all the finalizers that should be run before the program exits.
         I'm not sure what order they should be run in.
        '''
        return sum(map(lambda x: x.get_initializers(), self.all_objects), [])

    @property
    def linux_loader_object(self):
        for obj in self.all_objects:
            if obj.provides is None:
                continue
            if 'ld.so' in obj.provides or 'ld64.so' in obj.provides or 'ld-linux' in obj.provides:
                return obj
        return None

    def _load_main_binary(self):
        self.main_bin = self.load_object(self._main_binary_path, self._main_opts, is_main_bin=True)
        self.memory = Clemory(self.main_bin.arch, root=True)
        base_addr = self._main_opts.get('custom_base_addr', None)
        if base_addr is None and self.main_bin.requested_base is not None:
            base_addr = self.main_bin.requested_base
        if base_addr is None and self.main_bin.pic:
            base_addr = 0x400000
        if base_addr is None:
            base_addr = 0
        self.add_object(self.main_bin, base_addr)

    def _load_dependencies(self):
        while len(self._unsatisfied_deps) > 0:
            dep = self._unsatisfied_deps.pop(0)
            if os.path.basename(dep) in self._satisfied_deps:
                continue
            if self._ignore_import_version_numbers and dep.strip('.0123456789') in self._satisfied_deps:
                continue
            for path in self._possible_paths(dep):
                libname = os.path.basename(path)
                options = self._lib_opts.get(libname, {})
                try:
                    obj = self.load_object(path, options, compatible_with=self.main_bin)
                    break
                except (CLECompatibilityError, CLEFileNotFoundError):
                    continue
            else:
                if self._except_missing_libs:
                    raise CLEFileNotFoundError("Could not find shared library: %s" % dep)
                continue

            base_addr = options.get('custom_base_addr', None)
            self.add_object(obj, base_addr)
            self.shared_objects[obj.provides] = obj

    @staticmethod
    def load_object(path, options=None, compatible_with=None, is_main_bin=False):
        # Try to find the filetype of the object. Also detect if you were given a bad filepath
        try:
            filetype = Loader.identify_object(path)
        except OSError:
            raise CLEFileNotFoundError('File %s does not exist!' % path)

        # Verify that that filetype is acceptable
        if compatible_with is not None and filetype != compatible_with.filetype:
            raise CLECompatibilityError('File %s is not compatible with %s' % (path, compatible_with))

        # Check if the user specified a backend as...
        backend_option = options.get('backend', None)
        if isinstance(backend_option, type) and issubclass(backend_option, AbsObj):
            # ...an actual backend class
            backends = [backend_option]
        elif backend_option in BACKENDS:
            # ...the name of a backend class
            backends = [BACKENDS[backend_option]]
        elif isinstance(backend_option, (list, tuple)):
            # ...a list of backends containing either names or classes
            backends = []
            for backend_option_item in backend_option:
                if isinstance(backend_option_item, type) and issubclass(backend_option_item, AbsObj):
                    backends.append(backend_option_item)
                elif backend_option_item in BACKENDS:
                    backends.append(BACKENDS[backend_option_item])
                else:
                    raise CLEError('Invalid backend: %s' % backend_option_item)
        elif backend_option is None:
            backends = BACKENDS.values()
        else:
            raise CLEError('Invalid backend: %s' % backend_option)

        backends = filter(lambda x: filetype in x.supported_filetypes, backends)
        if len(backends) == 0:
            raise CLECompatibilityError('No compatible backends specified for filetype %s (file %s)' % (filetype, path))

        for backend in backends:
            try:
                loaded = backend(path, compatible_with=compatible_with, filetype=filetype, is_main_bin=is_main_bin, **options)
                return loaded
            except CLECompatibilityError:
                raise
            except CLEError:
                l.exception("Loading error when loading %s with backend %s", path, backend.__name__)
        raise CLEError("All backends failed loading %s!" % path)

    @staticmethod
    def identify_object(path):
        '''
         Returns the filetype of the file at path. Will be one of the strings
         in {'elf', 'pe', 'mach-o', 'unknown'}
        '''
        identstring = open(path, 'rb').read(0x1000)
        if identstring.startswith('\x7fELF'):
            return 'elf'
        elif identstring.startswith('MZ') and len(identstring) > 0x40:
            peptr = struct.unpack('I', identstring[0x3c:0x40])[0]
            if peptr < len(identstring) and identstring[peptr:peptr+4] == 'PE\0\0':
                return 'pe'
        elif identstring.startswith('\xfe\xed\xfa\xce') or \
             identstring.startswith('\xfe\xed\xfa\xcf') or \
             identstring.startswith('\xce\xfa\xed\xfe') or \
             identstring.startswith('\xcf\xfa\xed\xfe'):
            return 'mach-o'
        elif identstring.startswith('\x7fCGC'):
            return 'cgc'
        return 'unknown'

    def add_object(self, obj, base_addr=None):
        '''
         Add object obj to the memory map, rebased at base_addr.
         If base_addr is None CLE will pick a safe one.
         Registers all its dependencies.
        '''

        if self._auto_load_libs:
            self._unsatisfied_deps += obj.deps
        self.requested_objects.update(obj.deps)

        if obj.provides is not None:
            self._satisfied_deps.add(obj.provides)
            if self._ignore_import_version_numbers:
                self._satisfied_deps.add(obj.provides.strip('.0123456789'))

        self.all_objects.append(obj)

        if base_addr is None:
            if obj.requested_base is not None and self.addr_belongs_to_object(obj.requested_base) is None:
                base_addr = obj.requested_base
            else:
                base_addr = self._get_safe_rebase_addr()

        l.info("[Rebasing %s @%#x]", os.path.basename(obj.binary), base_addr)
        self.memory.add_backer(base_addr, obj.memory)
        obj.rebase_addr = base_addr

    def _possible_paths(self, path):
        if os.path.exists(path): yield path
        dirs = []                   # if we say dirs = blah, we modify the original
        dirs += self._custom_ld_path
        dirs += [os.path.dirname(self._main_binary_path)]
        dirs += self.main_bin.arch.library_search_path()
        for libdir in dirs:
            fullpath = os.path.realpath(os.path.join(libdir, path))
            if os.path.exists(fullpath): yield fullpath
            if self._ignore_import_version_numbers:
                try:
                    for libname in os.listdir(libdir):
                        if libname.strip('.0123456789') == path.strip('.0123456789'):
                            yield os.path.realpath(os.path.join(libdir, libname))
                except OSError: pass

    def _perform_reloc(self, obj):
        if obj.binary in self._relocated_objects:
            return
        self._relocated_objects.add(obj.binary)

        for dep_name in obj.deps:
            if dep_name not in self.shared_objects:
                continue
            dep_obj = self.shared_objects[dep_name]
            self._perform_reloc(dep_obj)

        if isinstance(obj, IDABin):
            pass
        elif isinstance(obj, (MetaELF, PE)):
            for reloc in obj.relocs:
                reloc.relocate(self.all_objects)

    def _get_safe_rebase_addr(self):
        """
        Get a "safe" rebase addr, i.e., that won't overlap with already loaded stuff.
        This is used as a fallback when we cannot use LD to tell use where to load
        a binary object. It is also a workaround to IDA crashes when we try to
        rebase binaries at too high addresses.
        """
        granularity = self._rebase_granularity
        return self.max_addr() + (granularity - self.max_addr() % granularity)

    def _load_tls(self):
        '''
         Set up an object to store TLS data in
        '''
        modules = []
        for obj in self.all_objects:
            if not isinstance(obj, MetaELF):
                continue
            if not obj.tls_used:
                continue
            modules.append(obj)
        if len(modules) == 0:
            return
        self.tls_object = TLSObj(modules)
        self.add_object(self.tls_object)

    def _finalize_tls(self):
        '''
         Lay out the TLS initialization images into memory
        '''
        if self.tls_object is not None:
            self.tls_object.finalize()

    def addr_belongs_to_object(self, addr):
        for obj in self.all_objects:
            if not (addr >= obj.get_min_addr() and addr < obj.get_max_addr()):
                continue

            if isinstance(obj.memory, str):
                return obj

            elif isinstance(obj.memory, Clemory):
                if addr - obj.rebase_addr in obj.memory:
                    return obj

            else:
                raise CLEError('Unsupported memory type %s' % type(obj.memory))

        return None

    def max_addr(self):
        """ The maximum address loaded as part of any loaded object
        (i.e., the whole address space)
        """
        return max(map(lambda x: x.get_max_addr(), self.all_objects))

    def min_addr(self):
        """ The minimum address loaded as part of any loaded object
        i.e., the whole address space)
        """
        return min(map(lambda x: x.get_min_addr(), self.all_objects))

    # Search functions

    def find_symbol_name(self, addr):
        """ Return the name of the function starting at addr.
        """
        for so in self.all_objects:
            if addr - so.rebase_addr in so.symbols_by_addr:
                return so.symbols_by_addr[addr - so.rebase_addr].name
        return None

    def find_plt_stub_name(self, addr):
        """ Return the name of the PLT stub starting at addr.
        """
        for so in self.all_objects:
            if isinstance(so, MetaELF):
                if addr in so.reverse_plt:
                    return so.reverse_plt[addr]
        return None

    def find_module_name(self, addr):
        for o in self.all_objects:
            # The Elf class only works with static non-relocated addresses
            if o.contains_addr(addr - o.rebase_addr):
                return os.path.basename(o.binary)

    def find_symbol_got_entry(self, symbol):
        """ Look for the address of a GOT entry for symbol @symbol.
        If found, return the address, otherwise, return None
        """
        if isinstance(self.main_bin, IDABin):
            if symbol in self.main_bin.imports:
                return self.main_bin.imports[symbol]
        elif isinstance(self.main_bin, ELF):
            if symbol in self.main_bin.jmprel:
                return self.main_bin.jmprel[symbol].addr

    def _ld_so_addr(self):
        """ Use LD_AUDIT to find object dependencies and relocation addresses"""

        qemu = 'qemu-%s' % self.main_bin.arch.qemu_name
        env_p = os.getenv("VIRTUAL_ENV", "/")
        bin_p = os.path.join(env_p, "local/lib", self.main_bin.arch.name.lower())

        # Our LD_AUDIT shared object
        ld_audit_obj = os.path.join(bin_p, "cle_ld_audit.so")

        #LD_LIBRARY_PATH
        ld_path = os.getenv("LD_LIBRARY_PATH")
        if ld_path == None:
            ld_path = bin_p
        else:
            ld_path = ld_path + ":" + bin_p

        cross_libs = self.main_bin.arch.lib_paths
        if self.main_bin.arch.name in ('AMD64', 'X86'):
            ld_libs = self.main_bin.arch.lib_paths
        elif self.main_bin.arch.name == 'PPC64':
            ld_libs = map(lambda x: x + 'lib64/', self.main_bin.arch.lib_paths)
        else:
            ld_libs = map(lambda x: x + 'lib/', self.main_bin.arch.lib_paths)
        ld_libs = ':'.join(ld_libs)
        ld_path = ld_path + ":" + ld_libs

        # Make LD look for custom libraries in the right place
        if self._custom_ld_path is not None:
            ld_path = self._custom_ld_path + ":" + ld_path

        var = "LD_LIBRARY_PATH=%s,LD_AUDIT=%s,LD_BIND_NOW=yes" % (ld_path, ld_audit_obj)

        # Let's work on a copy of the binary
        binary = self._binary_screwup_copy(self._main_binary_path)

        #LD_AUDIT's output
        log = "./ld_audit.out"

        cmd = [qemu, "-strace", "-L", cross_libs, "-E", var, binary]
        s = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)

        # Check stderr for library loading issues
        err = s.stderr.readlines()
        msg = "cannot open shared object file"

        deps = self.main_bin.deps

        for dep in deps:
            for str_e in err:
                if dep in str_e and msg in str_e:
                    l.error("LD could not find dependency %s.", dep)
                    l.error("GNU LD will stop looking for libraries to load if "
                            "it doesn't find one of them.")
                    #self.ld_missing_libs.append(dep)
                    break

        s.communicate()

        # Our LD_AUDIT library is supposed to generate a log file.
        # If not we're in trouble
        if os.path.exists(log):
            libs = {}
            f = open(log, 'r')
            for i in f.readlines():
                lib = i.split(",")
                if lib[0] == "LIB":
                    libs[lib[1]] = int(lib[2].strip(), 16)
            f.close()
            l.debug("---")
            for o, a in libs.iteritems():
                l.debug(" -> Dependency: %s @ 0x%x)", o, a)

            l.debug("---")
            os.remove(log)
            return libs

        else:

            l.error("Could not find library dependencies using ld."
                    " The log file '%s' does not exist, did qemu fail ? Try to run "
                    "`%s` manually to check", log, " ".join(cmd))
            raise CLEOperationError("Could not find library dependencies using ld.")

    def _binary_screwup_copy(self, path):
        """
        When LD_AUDIT cannot load CLE's auditing library, it unfortunately falls
        back to executing the target, which we don't want ! This is a problem
        specific to GNU LD, we can't fix this.

        This is a simple hack to work around it: set the address of the entry
        point to 0 in the program header
        This will cause the main binary to segfault if executed.
        """

        # Let's work on a copy of the main binary
        copy = self._make_tmp_copy(path, suffix=".screwed")
        f = open(copy, 'r+b')

        # Looking at elf.h, we can see that the the entry point's
        # definition is always at the same place for all architectures.
        off = 0x18
        f.seek(off)
        count = self.main_bin.arch.bits / 8

        # Set the entry point to address 0
        screw_char = "\x00"
        screw = screw_char * count
        f.write(screw)
        f.close()
        return copy

    @staticmethod
    def _make_tmp_copy(path, suffix=None):
        """ Makes a copy of obj into CLE's tmp directory """
        if not os.path.exists('/tmp/cle'):
            os.mkdir('/tmp/cle')
        if os.path.exists(path):
            bn = os.urandom(5).encode('hex')
            if suffix is not None:
                bn += suffix
            dest = os.path.join('/tmp/cle', bn)
            l.info("\t -> copy obj %s to %s", path, dest)
            shutil.copy(path, dest)
        else:
            raise CLEFileNotFoundError("File %s does not exist :(. Please check that the"
                                       " path is correct" % path)
        return dest

from .absobj import AbsObj
from .elf import ELF
from .metaelf import MetaELF
from .pe import PE
from .idabin import IDABin
from .blob import Blob
from .cgc import CGC
from .backedcgc import BackedCGC

BACKENDS = OrderedDict((
    ('elf', ELF),
    ('pe', PE),
    ('cgc', CGC),
    ('backedcgc', BackedCGC),
    ('ida', IDABin),
    ('blob', Blob)
))

