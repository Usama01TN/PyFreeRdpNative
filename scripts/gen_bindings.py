#!/usr/bin/env python3
"""
Generate ctypes bindings from the FreeRDP / winpr C headers.

    python scripts/gen_bindings.py \
        --include <src>/include --include <src>/winpr/include \
        --include <build>/include --include <build>/winpr/include \
        --out pyfreerdpnative

What comes out (one Python package):

    constants.py   #define integer/string constants and enum members
    types.py       typedefs, enums, structs and unions as ctypes classes,
                   function-pointer types
    functions.py   every FREERDP_API / WINPR_API prototype as
                   (restype, argtypes), plus bind(lib) to attach them

How it works: each public header is run through the C preprocessor
(`cpp`) with pycparser's fake libc headers, GCC-isms are neutralised, the
result is parsed with pycparser into a C AST, and the AST is walked to emit
Python. Nothing is hand-written per symbol, so re-running on a new FreeRDP
release regenerates everything.

Requirements: gcc (for cpp), pycparser.
"""

from __future__ import print_function

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    from pycparser import c_ast, c_parser
    HAVE_PYCPARSER = True
except ImportError:
    # Let the module import - and `--help` work - without pycparser: the
    # Emitter class below subclasses c_ast.NodeVisitor, so a stand-in base is
    # needed at class-definition time. Generating bindings checks
    # HAVE_PYCPARSER first and says what to install.
    HAVE_PYCPARSER = False
    c_parser = None

    class _StubAst(object):
        class NodeVisitor(object):
            def visit(self, node):
                raise RuntimeError("pycparser is required: pip install pycparser")

    c_ast = _StubAst()

# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

# C base types (after typedef resolution) -> ctypes expression.
BASE_TYPES = {
    "void": None,
    "char": "ctypes.c_char", "signed char": "ctypes.c_byte",
    "unsigned char": "ctypes.c_ubyte",
    "short": "ctypes.c_short", "short int": "ctypes.c_short",
    "signed short": "ctypes.c_short", "signed short int": "ctypes.c_short",
    "unsigned short": "ctypes.c_ushort", "unsigned short int": "ctypes.c_ushort",
    "int": "ctypes.c_int", "signed": "ctypes.c_int", "signed int": "ctypes.c_int",
    "unsigned": "ctypes.c_uint", "unsigned int": "ctypes.c_uint",
    "long": "ctypes.c_long", "long int": "ctypes.c_long",
    "signed long": "ctypes.c_long", "signed long int": "ctypes.c_long",
    "unsigned long": "ctypes.c_ulong", "unsigned long int": "ctypes.c_ulong",
    "long long": "ctypes.c_longlong", "long long int": "ctypes.c_longlong",
    "signed long long": "ctypes.c_longlong",
    "unsigned long long": "ctypes.c_ulonglong",
    "unsigned long long int": "ctypes.c_ulonglong",
    "float": "ctypes.c_float", "double": "ctypes.c_double",
    "long double": "ctypes.c_longdouble",
    "_Bool": "ctypes.c_bool", "bool": "ctypes.c_bool",
    "size_t": "ctypes.c_size_t", "ssize_t": "ctypes.c_ssize_t",
    "wchar_t": "ctypes.c_wchar",
    "int8_t": "ctypes.c_int8", "uint8_t": "ctypes.c_uint8",
    "int16_t": "ctypes.c_int16", "uint16_t": "ctypes.c_uint16",
    "int32_t": "ctypes.c_int32", "uint32_t": "ctypes.c_uint32",
    "int64_t": "ctypes.c_int64", "uint64_t": "ctypes.c_uint64",
    "intptr_t": "ctypes.c_ssize_t", "uintptr_t": "ctypes.c_size_t",
    "ptrdiff_t": "ctypes.c_ssize_t",
    "va_list": "ctypes.c_void_p", "__builtin_va_list": "ctypes.c_void_p",
    "FILE": None,   # opaque
}

# Headers whose declarations we emit (everything else is context only).
OUR_PREFIXES = ("freerdp/", "winpr/")

# Macros that expand to things pycparser cannot digest.
CPP_DEFINES = [
    "__attribute__(x)=", "__extension__=", "__inline=", "__inline__=",
    "__restrict=", "__restrict__=", "__asm__(x)=", "__asm(x)=",
    "__volatile__=", "__builtin_va_arg(a,b)=0", "__signed__=signed",
    "__thread=", "__declspec(x)=", "_Static_assert(a,b)=",
    "static_assert(a,b)=", "__typeof__(x)=int", "__auto_type=int",
    # winpr / freerdp export & attribute macros
    "WINPR_API=", "FREERDP_API=", "WINPR_LOCAL=", "FREERDP_LOCAL=",
    "WINPR_ATTR_MALLOC(x,y)=", "WINPR_ATTR_FORMAT_ARG(x,y)=",
    "WINPR_ATTR_NODISCARD=", "WINPR_ATTR_UNUSED=", "WINPR_NORETURN(x)=x",
    "WINPR_DEPRECATED(x)=x", "WINPR_DEPRECATED_VAR(m,x)=x",
    "WINPR_FALLTHROUGH=", "WINPR_PRAGMA_DIAG_PUSH=", "WINPR_PRAGMA_DIAG_POP=",
    "WINPR_PRAGMA_DIAG_IGNORED_UNUSED_CONST_VAR=",
    "WINPR_PRAGMA_DIAG_IGNORED_UNUSED_MACRO=",
    "WINPR_PRAGMA_DIAG_IGNORED_PEDANTIC=",
    "WINPR_PRAGMA_DIAG_IGNORED_MISSING_PROTOTYPES=",
    "WINPR_PRAGMA_DIAG_IGNORED_RESERVED_ID_MACRO=",
    "WINPR_PRAGMA_DIAG_IGNORED_UNUSED_MACROS=",
    "WINPR_PRAGMA_DIAG_TAUTOLOGICAL_CONSTANT_OUT_OF_RANGE_COMPARE=",
    "WINPR_PRAGMA_DIAG_IGNORED_RESERVED_IDENTIFIER=",
    "WINPR_PRAGMA_DIAG_IGNORED_ATOMIC_SEQ_CST=",
    "WINPR_PRAGMA_DIAG_IGNORED_UNUSED_BUT_SET_VARIABLE=",
    "WINPR_PRAGMA_DIAG_IGNORED_MISMATCHED_DEALLOC=",
    "WINPR_PRAGMA_DIAG_IGNORED_FORMAT_SECURITY=",
    "WINPR_PRAGMA_DIAG_IGNORED_QUALIFIERS=",
    "WINPR_PRAGMA_DIAG_IGNORED_STRICT_PROTOTYPES=",
    "WINPR_PRAGMA_DIAG_IGNORED_UNKNOWN_PRAGMAS=",
    "WINPR_PRAGMA_UNROLL_LOOP(x)=",
    "WINPR_RESTRICT=", "WINPR_STATIC_INLINE=static inline",
    "WINPR_ASSERTING_INT_CAST(t,v)=((t)(v))",
    "WINPR_CXX_COMPAT_CAST(t,v)=((t)(v))",
    "WINPR_STATIC_CAST(t,v)=((t)(v))",
    "WINPR_REINTERPRET_CAST(t,v)=((t)(v))",
    "WINPR_FUNC_ATTR_MALLOC(x,y)=",
    "WINPR_C_API_ENTRY=", "FREERDP_C_API_ENTRY=",
    # ALIGN64 fields. FreeRDP defines ALIGN64 as DECLSPEC_ALIGN(8) ->
    # __attribute__((aligned(8))), which every ALIGN64 field in rdpContext,
    # rdpSettings, freerdp_peer ... relies on: a 4-byte BOOL still occupies
    # an 8-byte slot. Stripping the attribute silently shifts every later
    # field. So the macro is mapped to the `restrict` qualifier - a token
    # pycparser keeps in the AST and FreeRDP never uses bare - and the
    # emitter turns any `restrict` field into an 8-byte-aligned union.
    "DECLSPEC_ALIGN(x)=restrict",
    "inline=",
]

# The channel headers refuse to be included unless their build flag is set
# (normally via buildflags.h of a build that enabled them). The bindings
# should describe every channel FreeRDP can have, so define them all.
CHANNELS = ["AINPUT", "AUDIN", "CLIPRDR", "DISP", "DRDYNVC", "DRIVE", "ECHO",
            "ENCOMSP", "GEOMETRY", "GFXREDIR", "LOCATION", "PARALLEL",
            "PRINTER", "RAIL", "RDP2TCP", "RDPDR", "RDPEAR", "RDPECAM",
            "RDPEI", "RDPEMSC", "RDPGFX", "RDPSND", "REMDESK", "SERIAL",
            "SMARTCARD", "SSHAGENT", "TELEMETRY", "TSMF", "URBDRC", "VIDEO"]
for _ch in CHANNELS:
    CPP_DEFINES += ["CHANNEL_{0}=1".format(_ch), "CHANNEL_{0}_CLIENT=1".format(_ch),
                    "CHANNEL_{0}_SERVER=1".format(_ch)]
# ...and the umbrella flags some declarations are guarded by (e.g.
# freerdp_channels_load_static_addin_entry sits behind WITH_CHANNELS).
CPP_DEFINES += ["WITH_CHANNELS=1", "WITH_CLIENT_CHANNELS=1", "WITH_SERVER_CHANNELS=1",
                "WITH_CLIENT_COMMON=1", "WITH_CLIENT_INTERFACE=1"]
# pycparser's stub <limits.h> lacks CHAR_BIT; without it wtypes.h picks
# `typedef int8_t CHAR` and every LPSTR/LPCSTR becomes POINTER(c_byte)
# instead of c_char_p.
CPP_DEFINES += ["CHAR_BIT=8"]

# Symbols that exist in the AST but should never be emitted.
SKIP_NAMES = {"main"}

# Headers whose names collide with macros from other headers (e.g.
# winpr/asn1.h's enum ER_TAG_BOOLEAN vs freerdp/crypto/er.h's #define of the
# same name). Real code never includes both; here each gets its own
# translation unit and the results are merged.
SEPARATE_TUS = [["winpr/asn1.h"]]

# Constants that describe the machine that ran `cmake`, not FreeRDP's API:
# the detected features (FREERDP_BUILD_CONFIG, WINPR_HAVE_*), the git revision
# of the checkout ('n/a' for a tarball). They differ from run to run, so they
# are never emitted; struct layouts and prototypes do not depend on them.
ENVIRONMENT_HEADERS = re.compile(r"(^|/)(freerdp|winpr)/(buildflags|build-config|config)\.h$")
ENVIRONMENT_MACROS = re.compile(r"^(FREERDP|WINPR)_(GIT_REVISION|BUILD_CONFIG)$|^WINPR_HAVE_")

# Headers that cannot be included standalone / are not public API.
EXCLUDE_HEADERS = re.compile(
    r"(^|/)(private/|.*_private\.h$|winpr/tools/|freerdp/client/utils/|"
    r"winpr/intrin\.h$|winpr/pack\.h$|winpr/cast\.h$|freerdp/utils/pod_arrays\.h$|"
    r"freerdp/utils/warnings\.h$|freerdp/client/file\.h$)")

# Types the fake libc stubs do not provide but the headers reference.
PRELUDE = """
typedef struct { long fds_bits[16]; } fd_set;
typedef unsigned int socklen_t;
typedef int pid_t;
typedef long off_t;
typedef long time_t;
typedef long suseconds_t;
typedef unsigned long pthread_t;
struct timeval { long tv_sec; long tv_usec; };
struct timespec { long tv_sec; long tv_nsec; };
struct sockaddr { unsigned short sa_family; char sa_data[14]; };
struct sockaddr_storage { unsigned short ss_family; char __pad[126]; };
struct in_addr { unsigned int s_addr; };
struct in6_addr { unsigned char s6_addr[16]; };
struct sockaddr_in { unsigned short sin_family; unsigned short sin_port; struct in_addr sin_addr; unsigned char sin_zero[8]; };
struct sockaddr_in6 { unsigned short sin6_family; unsigned short sin6_port; unsigned int sin6_flowinfo; struct in6_addr sin6_addr; unsigned int sin6_scope_id; };
struct iovec { void* iov_base; unsigned long iov_len; };
struct pollfd { int fd; short events; short revents; };
struct tm { int tm_sec, tm_min, tm_hour, tm_mday, tm_mon, tm_year, tm_wday, tm_yday, tm_isdst; };
"""


# ---------------------------------------------------------------------------
# Preprocessing + parsing
# ---------------------------------------------------------------------------

def find_fake_libc(explicit=None):
    """
    pycparser's stub system headers. Recent pip packages of pycparser do not
    ship them, so: use --fake-libc if given, else a cached clone of the
    pycparser repository (utils/fake_libc_include), fetching it if needed.
    """
    if explicit:
        return os.path.abspath(explicit)
    import pycparser
    cand = os.path.join(os.path.dirname(pycparser.__file__), "..", "utils",
                        "fake_libc_include")
    if os.path.isdir(cand):
        return os.path.abspath(cand)
    cache = os.path.join(os.path.expanduser("~"), ".cache", "pycparser-fake-libc")
    inc = os.path.join(cache, "utils", "fake_libc_include")
    if not os.path.isdir(inc):
        print("[gen] fetching pycparser fake libc headers into {0}".format(cache))
        subprocess.check_call(["git", "clone", "-q", "--depth", "1",
                               "https://github.com/eliben/pycparser", cache])
    return inc if os.path.isdir(inc) else None


def preprocess(header, includes, fake_libc, extra_defines=()):
    # NB: no -P. The "# <line> <file>" markers are what give pycparser real
    # coordinates (parse errors with a location, and the header each
    # declaration came from).
    cmd = ["cpp", "-E", "-nostdinc", "-std=c11", "-D__STDC__=1",
           "-D__x86_64__=1", "-D__linux__=1", "-D__GNUC__=4", "-D__GNUC_MINOR__=8",
           "-D_GNU_SOURCE=1", "-D__SIZEOF_POINTER__=8",
           "-D__SIZE_TYPE__=unsigned long", "-D__PTRDIFF_TYPE__=long",
           "-D__WCHAR_TYPE__=int", "-D__CHAR16_TYPE__=unsigned short",
           "-D__CHAR32_TYPE__=unsigned int", "-D__INTPTR_TYPE__=long",
           "-D__UINTPTR_TYPE__=unsigned long", "-D__INT64_TYPE__=long",
           "-D__UINT64_TYPE__=unsigned long", "-D__INT32_TYPE__=int",
           "-D__UINT32_TYPE__=unsigned int", "-D__INT16_TYPE__=short",
           "-D__UINT16_TYPE__=unsigned short", "-D__INT8_TYPE__=signed char",
           "-D__UINT8_TYPE__=unsigned char"]
    for d in CPP_DEFINES:
        cmd += ["-D" + d]
    for d in extra_defines:
        cmd += ["-D" + d]
    if fake_libc:
        cmd += ["-I", fake_libc]
    for inc in includes:
        cmd += ["-I", inc]
    cmd.append(header)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_b, err_b = proc.communicate()
    if proc.returncode != 0:
        err = err_b.decode("utf-8", "replace")
        sys.exit("preprocessing failed:\n" + "\n".join(
            line for line in err.splitlines() if "error" in line)[:4000])
    out = out_b.decode("utf-8", "replace")
    # A couple of leftovers pycparser still rejects.
    out = re.sub(r"__attribute__\s*\(\([^()]*(\([^()]*\))?[^()]*\)\)", "", out)
    # The ALIGN64 marker is only meaningful on fields; at struct level
    # (`typedef struct DECLSPEC_ALIGN(8) {`) drop it - those structs hold
    # pointers / 64-bit members and are 8-aligned regardless.
    out = re.sub(r"\b(struct|union)\s+restrict\b", r"\1", out)
    out = re.sub(r"\b__asm__\s*\([^)]*\)", "", out)
    out = re.sub(r"\b_Alignas\s*\([^)]*\)", "", out)
    out = re.sub(r"\b_Alignof\s*\([^)]*\)", "8", out)
    out = re.sub(r"\b__alignof__\s*\([^)]*\)", "8", out)
    return out


def run_cpp(cmd, what):
    """Run a cpp command; on failure show ITS errors, not a bare CalledProcessError."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        errors = [line for line in err.splitlines() if "error" in line.lower()]
        sys.exit("cpp failed while {0} (exit {1}):\n{2}".format(
            what, proc.returncode, "\n".join(errors[:20] or err.splitlines()[-20:])))
    return proc.stdout.decode("utf-8", "replace")


def macro_origins(header, includes, fake_libc):
    """
    name -> relative header path for every object-like #define, using
    `cpp -dD` (keeps the #define directives in place, between the
    `# <line> "<file>"` markers).
    """
    cmd = ["cpp", "-E", "-dD", "-nostdinc", "-std=c11", "-D__STDC__=1",
           "-D__x86_64__=1", "-D__linux__=1"]
    for d in CPP_DEFINES:
        cmd += ["-D" + d]
    if fake_libc:
        cmd += ["-I", fake_libc]
    for inc in includes:
        cmd += ["-I", inc]
    cmd.append(header)
    out = run_cpp(cmd, "locating macro definitions")
    abs_incs = [os.path.abspath(i) for i in includes]
    origins, cur = {}, None
    for line in out.splitlines():
        m = re.match(r'# \d+ "([^"]*)"', line)
        if m:
            f = os.path.abspath(m.group(1))
            cur = None
            for inc in abs_incs:
                if f.startswith(inc + os.sep):
                    cur = os.path.relpath(f, inc).replace(os.sep, "/")
                    break
            continue
        m = re.match(r"#define\s+([A-Za-z_]\w*)(?![\w(])", line)
        if m and cur:
            origins[m.group(1)] = cur
    return origins


def constants_from_macros(header, includes, fake_libc):
    """
    Integer / string #defines visible after including the header, taken
    from cpp -dM. Only macros defined in our own headers are kept; values
    that are not literals (or arithmetic of literals) are dropped.
    """
    cmd = ["cpp", "-E", "-dM", "-P", "-nostdinc", "-std=c11", "-D__STDC__=1",
           "-D__x86_64__=1", "-D__linux__=1"]
    for d in CPP_DEFINES:
        cmd += ["-D" + d]
    if fake_libc:
        cmd += ["-I", fake_libc]
    for inc in includes:
        cmd += ["-I", inc]
    cmd.append(header)
    out = run_cpp(cmd, "collecting #defines")
    macros = {}
    for line in out.splitlines():
        m = re.match(r"#define\s+([A-Za-z_]\w*)\s*(.*)$", line)
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip()
        if name.startswith("_") or "(" in name:
            continue
        macros[name] = value
    return macros


def expand_macros(header, includes, fake_libc, names):
    """
    Let cpp expand each object-like macro fully (this resolves values built
    from function-like macros, e.g. PIXEL_FORMAT_BGRX32 ->
    FREERDP_PIXEL_FORMAT(32, ...) -> arithmetic on literals). Returns
    name -> expanded text.
    """
    marker = "@@GEN@@"
    # absolute: cpp resolves a quoted #include relative to the including
    # file's directory, so "pyfreerdpnative/_all_roots_0.c" from a file that
    # lives in pyfreerdpnative/ is looked up as pyfreerdpnative/pyfreerdpnative/...
    body = ["#include \"{0}\"".format(os.path.abspath(header))]
    for n in names:
        # the first copy is a string literal so cpp leaves it alone
        body.append('{0} "{1}" {2}'.format(marker, n, n))
    tmp = header + ".expand.c"
    with open(tmp, "w") as fh:
        fh.write("\n".join(body) + "\n")
    cmd = ["cpp", "-E", "-P", "-nostdinc", "-std=c11", "-D__STDC__=1",
           "-D__x86_64__=1", "-D__linux__=1"]
    for d in CPP_DEFINES:
        cmd += ["-D" + d]
    if fake_libc:
        cmd += ["-I", fake_libc]
    for inc in includes:
        cmd += ["-I", inc]
    cmd.append(tmp)
    try:
        out = run_cpp(cmd, "expanding macros")
    finally:
        os.remove(tmp)
    expanded = {}
    for line in out.splitlines():
        if line.startswith(marker):
            m = re.match(r'\s*"([A-Za-z_]\w*)"\s*(.*)$', line[len(marker):])
            if m and m.group(2).strip() and m.group(2).strip() != m.group(1):
                expanded[m.group(1)] = m.group(2).strip()
    return expanded


_EXPR_PARSER = None


def _eval_c_expr(text, known):
    """Evaluate an integer constant expression with pycparser's AST."""
    global _EXPR_PARSER
    if _EXPR_PARSER is None:
        _EXPR_PARSER = c_parser.CParser()
    # give every known constant a declaration so identifiers parse, then
    # evaluate the initializer with the shared const_expr logic
    decls = "".join("int {0};".format(k) for k in re.findall(r"[A-Za-z_]\w*", text)
                    if k in known and not k.startswith("__"))
    try:
        ast = _EXPR_PARSER.parse(decls + "int __gen_value = ({0});".format(text))
    except Exception:
        return None
    init = None
    for node in ast.ext:
        if isinstance(node, c_ast.Decl) and node.name == "__gen_value":
            init = node.init
    if init is None:
        return None
    em = Emitter([])
    em.int_constants = {k: v for k, v in known.items() if isinstance(v, int)}
    return em.const_expr(init)


def eval_macro(value, known):
    """Turn a macro body into a Python literal if it is a constant, else None."""
    v = value.strip()
    if not v:
        return None
    m = re.match(r'^L?"((?:[^"\\]|\\.)*)"$', v)          # string literal
    if m:
        return repr(m.group(1))
    m = re.match(r"^L?'((?:[^'\\]|\\.))'$", v)          # char literal
    if m:
        return repr(m.group(1))
    if re.search(r"[A-Za-z_]\w*\s*\(", re.sub(r"\(\s*(?:const\s+)?(?:unsigned\s+|signed\s+)?[A-Za-z_]\w*\s*\**\s*\)", "", v)):
        # still contains a call (unexpanded function-like macro / real call)
        return None
    v = v.replace("TRUE", "1").replace("FALSE", "0").replace("NULL", "0")
    # identifiers only - numeric literals (0x8000u, 12UL) must not be scanned,
    # or "x8000" / "UL" look like unknown names
    ident_scan = re.sub(r"\b0[xX][0-9A-Fa-f]+[uUlL]*\b|\b\d+[uUlL]*\b", " ", v)
    for tok in set(re.findall(r"[A-Za-z_]\w*", ident_scan)):
        if tok in known and not isinstance(known[tok], int):
            return None                        # string-valued reference
        if tok not in known and not re.match(
                r"^(unsigned|signed|int|long|short|char|const|UINT8|UINT16|UINT32|UINT64|INT8|INT16|INT32|INT64|BYTE|DWORD|WORD|ULONG|LONG|SIZE_T|size_t|BOOL|u?int\d+_t|wchar_t|WCHAR|CHAR)$",
                tok):
            return None                        # unknown identifier
    result = _eval_c_expr(v, known)
    if result is None:
        return None
    return repr(int(result))


# ---------------------------------------------------------------------------
# AST -> Python
# ---------------------------------------------------------------------------

class Emitter(c_ast.NodeVisitor):
    def __init__(self, our_files):
        self.our_files = our_files
        self.typedefs = {}       # name -> ctypes expr (or struct class name)
        self.enums = []          # (enum name or None, [(member, value)])
        self.structs = {}        # tag -> (kind, [(fname, ctype, bits)], declared_here)
        self.struct_order = []   # emission order
        self.func_ptr_types = {} # typedef name -> (restype, argtypes)
        self.functions = {}      # name -> (restype, argtypes, varargs)
        self.anon_counter = 0
        self._enum_value = 0
        self.pack_stack = []
        self.current_pack = None
        self.struct_pack = {}    # tag -> pack value
        # origin header (relative path like "freerdp/codec/color.h") of
        # every emitted name, so the 1:1 header mirror can be produced
        self.origin = {"typedef": {}, "struct": {}, "enum": {}, "func": {}}
        self.include_dirs = []

    def rel_header(self, node):
        c = getattr(node, "coord", None)
        if not c or not c.file:
            return None
        f = os.path.abspath(c.file)
        for inc in self.include_dirs:
            if f.startswith(inc + os.sep):
                return os.path.relpath(f, inc).replace(os.sep, "/")
        return None

    # -- helpers --------------------------------------------------------
    def in_our_file(self, node):
        c = getattr(node, "coord", None)
        if not c or not c.file:
            return False
        f = c.file.replace("\\", "/")
        return any(("/include/" + p) in f or f.startswith(p) for p in OUR_PREFIXES) \
            or any(os.path.abspath(f).startswith(o) for o in self.our_files)

    def ctype_of(self, node, quals=()):
        """ctypes expression for a pycparser type node."""
        if isinstance(node, c_ast.TypeDecl):
            return self.ctype_of(node.type)
        if isinstance(node, c_ast.IdentifierType):
            name = " ".join(node.names)
            if name in BASE_TYPES:
                return BASE_TYPES[name]
            if name in self.typedefs:
                return name          # emitted typedef alias
            return "ctypes.c_void_p" if name.endswith("_t") is False and False else \
                self.typedefs.get(name, "ctypes.c_int")
        if isinstance(node, c_ast.PtrDecl):
            inner = self.ctype_of(node.type)
            # char* -> c_char_p, void* -> c_void_p, function pointers stay
            if isinstance(node.type, c_ast.FuncDecl):
                return inner
            if inner is None:
                return "ctypes.c_void_p"
            # follow typedef aliases (CHAR -> ctypes.c_char) so LPSTR/LPCSTR
            # and friends become c_char_p, which accepts Python bytes
            base, seen = inner, set()
            while base in self.typedefs and base not in seen:
                seen.add(base)
                base = self.typedefs[base]
            if base == "ctypes.c_char" or inner == "ctypes.c_char":
                return "ctypes.c_char_p"
            if base == "ctypes.c_wchar":
                return "ctypes.c_wchar_p"
            if inner == "ctypes.c_wchar":
                return "ctypes.c_wchar_p"
            if inner in ("WCHAR", "wchar_t"):
                return "ctypes.c_void_p"
            return "ctypes.POINTER({0})".format(inner)
        if isinstance(node, c_ast.ArrayDecl):
            inner = self.ctype_of(node.type)
            if inner is None:
                inner = "ctypes.c_ubyte"
            dim = self.const_expr(node.dim)
            if dim is None:
                return "ctypes.POINTER({0})".format(inner)
            return "({0} * {1})".format(inner, dim)
        if isinstance(node, c_ast.FuncDecl):
            res = self.ctype_of(node.type)
            args = []
            if node.args:
                for p in node.args.params:
                    if isinstance(p, c_ast.EllipsisParam):
                        continue           # variadic: not expressible in CFUNCTYPE
                    t = self.ctype_of(p.type) if not isinstance(p, c_ast.ID) else "ctypes.c_int"
                    if t is None:            # (void)
                        continue
                    args.append(t)
            return "ctypes.CFUNCTYPE({0}{1})".format(
                res if res is not None else "None",
                "".join(", " + a for a in args))
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            tag = node.name
            if tag is None:
                self.anon_counter += 1
                tag = "_anon_{0}".format(self.anon_counter)
                node.name = tag
            if node.decls is not None:
                self.register_struct(node, tag)
            elif tag not in self.structs:
                self.structs[tag] = (
                    "union" if isinstance(node, c_ast.Union) else "struct",
                    None, False)
                self.struct_order.append(tag)
            return self.struct_class_name(tag)
        if isinstance(node, c_ast.Enum):
            self.register_enum(node)
            return "ctypes.c_int"
        if isinstance(node, c_ast.Typename):
            return self.ctype_of(node.type)
        if isinstance(node, c_ast.Decl):
            return self.ctype_of(node.type)
        return "ctypes.c_void_p"

    @staticmethod
    def struct_class_name(tag):
        return "struct_" + tag if not tag.startswith("_anon_") else tag

    @staticmethod
    def _is_align64(decl):
        """True if the declaration carries the ALIGN64 marker qualifier."""
        def quals_of(n):
            q = set(getattr(n, "quals", []) or [])
            t = getattr(n, "type", None)
            while t is not None and not isinstance(t, (c_ast.Struct, c_ast.Union,
                                                       c_ast.Enum, c_ast.IdentifierType)):
                q |= set(getattr(t, "quals", []) or [])
                t = getattr(t, "type", None)
            return q
        return "restrict" in quals_of(decl)

    def register_struct(self, node, tag):
        kind = "union" if isinstance(node, c_ast.Union) else "struct"
        fields = []
        for d in node.decls or []:
            if isinstance(d, c_ast.Pragma):
                continue
            fname = d.name
            bits = self.const_expr(d.bitsize) if getattr(d, "bitsize", None) else None
            ftype = self.ctype_of(d.type)
            if ftype is None:
                ftype = "ctypes.c_void_p"
            if fname is None:                  # anonymous nested struct/union
                fname = "_anon_field_{0}".format(len(fields))
                anon = True
            else:
                anon = False
            if self._is_align64(d) and kind == "struct" and bits is None:
                # emit as an anonymous 8-byte-aligned union carrying the field
                fields.append(("_a64_" + fname,
                               "_align64({0!r}, {1})".format(fname, ftype),
                               None, True))
                continue
            fields.append((fname, ftype, bits, anon))
        self.structs[tag] = (kind, fields, True)
        h = self.rel_header(node)
        if h:
            self.origin["struct"][tag] = h
        if self.current_pack:
            self.struct_pack[tag] = self.current_pack
        if tag not in self.struct_order:
            self.struct_order.append(tag)

    def register_enum(self, node):
        members = []
        value = 0
        if node.values:
            for e in node.values.enumerators:
                if e.value is not None:
                    v = self.const_expr(e.value, members)
                    if v is None:
                        v = value
                    value = v
                members.append((e.name, value))
                value += 1
        self.enums.append((node.name, members))
        h = self.rel_header(node)
        if h:
            for m, _v in members:
                self.origin["enum"][m] = h

    def const_expr(self, node, members=None):
        """Evaluate a constant expression from the AST; None if not constant."""
        if node is None:
            return None
        env = dict(members or [])
        for _n, mem in self.enums:
            env.update(dict(mem))
        env.update(self.int_constants)

        def ev(n):
            if isinstance(n, c_ast.Constant):
                v = n.value
                if n.type == "char":
                    return ord(eval(v))
                v = re.sub(r"[uUlL]+$", "", v)
                return int(v, 0)
            if isinstance(n, c_ast.ID):
                if n.name in env:
                    return env[n.name]
                raise ValueError(n.name)
            if isinstance(n, c_ast.UnaryOp):
                if n.op == "sizeof":
                    return self.sizeof_node(n.expr)
                x = ev(n.expr)
                return {"-": -x, "+": x, "~": ~x, "!": int(not x)}[n.op]
            if isinstance(n, c_ast.BinaryOp):
                a, b = ev(n.left), ev(n.right)
                return {"+": a + b, "-": a - b, "*": a * b, "/": a // b if b else 0,
                        "%": a % b if b else 0, "<<": a << b, ">>": a >> b,
                        "|": a | b, "&": a & b, "^": a ^ b,
                        "&&": int(bool(a and b)), "||": int(bool(a or b)),
                        "==": int(a == b), "!=": int(a != b), "<": int(a < b),
                        ">": int(a > b), "<=": int(a <= b), ">=": int(a >= b)}[n.op]
            if isinstance(n, c_ast.Cast):
                return ev(n.expr)
            if isinstance(n, c_ast.UnaryOp) and n.op == "sizeof":
                raise ValueError("sizeof")     # handled below
            if isinstance(n, c_ast.TernaryOp):
                return ev(n.iftrue) if ev(n.cond) else ev(n.iffalse)
            raise ValueError(type(n).__name__)
        try:
            return ev(node)
        except Exception:
            return None

    int_constants = {}

    def sizeof_node(self, node):
        """sizeof(<scalar type>) at generation time; struct sizes unsupported."""
        import ctypes as _ct
        expr = self.ctype_of(node.type if isinstance(node, c_ast.Typename) else node)
        seen = set()
        while expr in self.typedefs and expr not in seen:   # follow aliases
            seen.add(expr)
            expr = self.typedefs[expr]
        if expr is None:
            raise ValueError("sizeof void")
        if expr.startswith("ctypes."):
            return _ct.sizeof(eval(expr, {"ctypes": _ct}))
        raise ValueError("sizeof of non-scalar: " + str(expr))

    # -- visitors -------------------------------------------------------
    def visit_Typedef(self, node):
        name = node.name
        if name in BASE_TYPES:
            return
        h = self.rel_header(node)
        if h:   # a typedef may be (forward-)declared in several headers
            self.origin["typedef"].setdefault(name, set()).add(h)
        t = node.type
        if isinstance(t, c_ast.TypeDecl) and isinstance(t.type, (c_ast.Struct, c_ast.Union)):
            tag = t.type.name or name
            if t.type.name is None:
                t.type.name = tag
            expr = self.ctype_of(t.type)
            self.typedefs[name] = expr
            return
        if isinstance(t, c_ast.TypeDecl) and isinstance(t.type, c_ast.Enum):
            if t.type.name is None:
                t.type.name = name
            self.register_enum(t.type)
            self.typedefs[name] = "ctypes.c_int"
            return
        if isinstance(t, c_ast.PtrDecl) and isinstance(t.type, c_ast.FuncDecl):
            self.typedefs[name] = self.ctype_of(t.type)
            return
        expr = self.ctype_of(t)
        self.typedefs[name] = expr if expr is not None else "None"

    def visit_Decl(self, node):
        if isinstance(node.type, c_ast.FuncDecl):
            if not self.in_our_file(node) or node.name in SKIP_NAMES:
                return
            if "static" in (node.storage or []) or "inline" in (node.funcspec or []):
                return
            fd = node.type
            res = self.ctype_of(fd.type)
            args, varargs = [], False
            if fd.args:
                for p in fd.args.params:
                    if isinstance(p, c_ast.EllipsisParam):
                        varargs = True
                        continue
                    t = self.ctype_of(p.type)
                    if t is None:
                        continue
                    args.append(t)
            self.functions[node.name] = (res, args, varargs)
            h = self.rel_header(node)
            if h:
                self.origin["func"][node.name] = h
            return
        # struct / union / enum declarations without typedef
        if isinstance(node.type, (c_ast.Struct, c_ast.Union)):
            if node.type.decls is not None:
                self.ctype_of(node.type)
        elif isinstance(node.type, c_ast.Enum):
            self.register_enum(node.type)
        elif isinstance(node.type, c_ast.TypeDecl) and isinstance(
                node.type.type, (c_ast.Struct, c_ast.Union, c_ast.Enum)):
            self.ctype_of(node.type.type)

    def visit_Pragma(self, node):
        s = (node.string or "").replace(" ", "")
        m = re.match(r"pack\(push,(\d+)\)", s)
        if m:
            self.pack_stack.append(self.current_pack)
            self.current_pack = int(m.group(1))
        elif s.startswith("pack(pop"):
            self.current_pack = self.pack_stack.pop() if self.pack_stack else None
        elif re.match(r"pack\((\d+)\)", s):
            self.current_pack = int(re.match(r"pack\((\d+)\)", s).group(1))
        elif s == "pack()":
            self.current_pack = None

    def visit_FuncDef(self, node):
        # static inline helpers in headers: not exported, skip the body but
        # still learn any types declared inside its prototype
        pass


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

HEADER = '''"""
GENERATED by scripts/gen_bindings.py from the FreeRDP {version} headers.
Do not edit by hand - re-run the generator.
"""
'''


def emit(em, macros_all, out_dir, version):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # ---- constants ---------------------------------------------------
    lines = [HEADER.format(version=version), "", "# --- #define constants ---"]
    known = {}
    # resolve in dependency order: iterate until no progress
    pending = dict(macros_all)
    for _ in range(6):
        progress = False
        for name in sorted(pending):
            lit = eval_macro(pending[name], known)
            if lit is not None:
                known[name] = eval(lit) if not lit.startswith("'") else lit.strip("'")
                lines.append("{0} = {1}".format(name, lit))
                del pending[name]
                progress = True
        if not progress:
            break
    em.int_constants = {k: v for k, v in known.items() if isinstance(v, int)}
    lines.append("")
    lines.append("# --- enum members ---")
    seen = set()
    for ename, members in em.enums:
        if ename:
            lines.append("# enum {0}".format(ename))
        for m, v in members:
            if m in seen:
                continue
            seen.add(m)
            lines.append("{0} = {1}".format(m, v))
    with open(os.path.join(out_dir, "constants.py"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    # ---- types -------------------------------------------------------
    lines = [HEADER.format(version=version), "import ctypes", "",
             "from . import constants as _c  # noqa: F401", "",
             "",
             "def _align64(name, ctype):",
             '    """ALIGN64 field: an anonymous union that is 8-byte aligned and at',
             '    least 8 bytes, exactly what DECLSPEC_ALIGN(8) produces in C."""',
             "    return type('_Align64_' + name, (ctypes.Union,),",
             "                {'_fields_': [(name, ctype), ('_pad', ctypes.c_uint64)]})",
             ""]
    # forward-declare every struct class so pointers can reference them
    lines.append("# --- struct / union forward declarations ---")
    for tag in em.struct_order:
        kind, fields, _here = em.structs[tag]
        base = "ctypes.Union" if kind == "union" else "ctypes.Structure"
        lines.append("class {0}({1}):".format(em.struct_class_name(tag), base))
        lines.append("    pass")
    lines.append("")
    lines.append("# --- typedefs ---")
    # typedefs may reference each other; emit in dependency-safe order by
    # retrying until stable
    pending = dict(em.typedefs)
    emitted = set()
    # Arrays of structs must wait until the struct has its layout: ctypes
    # caches `(T * n)`, so an array type created while T is still empty
    # would keep size 0 everywhere it is used later.
    struct_classes = {em.struct_class_name(t) for t in em.struct_order}

    def is_struct_array(expr):
        m = re.match(r"^\(\s*([A-Za-z_]\w*)\s*\*\s*\d+\s*\)$", expr or "")
        if not m:
            return False
        base = m.group(1)
        seen = set()
        while base in em.typedefs and base not in seen and base not in struct_classes:
            seen.add(base)
            base = em.typedefs[base]
        return base in struct_classes

    deferred = {n: e for n, e in pending.items() if is_struct_array(e)}
    for n in deferred:
        del pending[n]
    for _ in range(10):
        progress = False
        for name in list(pending):
            expr = pending[name]
            refs = set(re.findall(r"\b([A-Za-z_]\w*)\b", expr or "")) - {
                "ctypes", "POINTER", "CFUNCTYPE", "None"}
            refs = {r for r in refs if not r.startswith("c_") and r in em.typedefs
                    and r not in emitted and r != name}
            if refs:
                continue
            lines.append("{0} = {1}".format(name, expr))
            emitted.add(name)
            del pending[name]
            progress = True
        if not progress:
            break
    for name in pending:      # cyclic leftovers: fall back to void*
        lines.append("{0} = ctypes.c_void_p  # unresolved: {1}".format(
            name, pending[name]))
    lines.append("")
    lines.append("# --- struct / union layouts (by-value dependencies first) ---")

    class_names = {em.struct_class_name(t) for t in em.struct_order}

    def by_value_deps(fields):
        """Struct classes embedded by value (not behind POINTER/CFUNCTYPE)."""
        deps = set()
        for _f, ftype, _b, _a in fields:
            # drop anything inside POINTER(...) / CFUNCTYPE(...): those only
            # need the class object to exist, not its layout
            stripped = re.sub(r"ctypes\.(POINTER|CFUNCTYPE)\((?:[^()]|\([^()]*\))*\)",
                              "", ftype)      # _align64('x', T) keeps T visible
            for tok in re.findall(r"[A-Za-z_]\w*", stripped):
                if tok in class_names:
                    deps.add(tok)
                elif tok in em.typedefs and em.typedefs[tok] in class_names:
                    deps.add(em.typedefs[tok])
        return deps

    pending = [t for t in em.struct_order if em.structs[t][1]]
    for t in em.struct_order:
        if not em.structs[t][1]:
            lines.append("# {0} {1}: opaque".format(em.structs[t][0], t))
    done = set()
    ordered = []
    for _ in range(50):
        progress = False
        for t in list(pending):
            deps = by_value_deps(em.structs[t][1]) - {em.struct_class_name(t)}
            if all(d in done for d in deps):
                ordered.append(t)
                done.add(em.struct_class_name(t))
                pending.remove(t)
                progress = True
        if not pending or not progress:
            break
    if pending:
        lines.append("# WARNING: cyclic by-value dependencies, emitted as-is: {0}".format(
            ", ".join(pending)))
        ordered += pending

    for tag in ordered:
        kind, fields, here = em.structs[tag]
        cls = em.struct_class_name(tag)
        if not fields:
            continue
        if tag in em.struct_pack:
            lines.append("{0}._pack_ = {1}".format(cls, em.struct_pack[tag]))
        anon = [f for f, _t, _b, a in fields if a]
        if anon:
            lines.append("{0}._anonymous_ = ({1},)".format(
                cls, ", ".join(repr(a) for a in anon)))
        lines.append("{0}._fields_ = [".format(cls))
        for fname, ftype, bits, _a in fields:
            if bits is not None:
                lines.append("    ({0!r}, {1}, {2}),".format(fname, ftype, bits))
            else:
                lines.append("    ({0!r}, {1}),".format(fname, ftype))
        lines.append("]")
    if deferred:
        lines.append("")
        lines.append("# --- array-of-struct typedefs (after the layouts they depend on) ---")
        for name in sorted(deferred):
            lines.append("{0} = {1}".format(name, deferred[name]))
    with open(os.path.join(out_dir, "types.py"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    # ---- functions ---------------------------------------------------
    lines = [HEADER.format(version=version), "import ctypes", "",
             "from .types import *  # noqa: F401,F403", "",
             "# name -> (restype, argtypes, variadic)", "PROTOTYPES = {"]
    for name in sorted(em.functions):
        res, args, varargs = em.functions[name]
        lines.append("    {0!r}: ({1}, [{2}], {3}),".format(
            name, res if res is not None else "None", ", ".join(args), varargs))
    lines.append("}")
    lines.append('''

def bind(lib, names=None, strict=False):
    """
    Attach restype/argtypes to the functions of an already-loaded ctypes
    library. `names` limits the set; missing symbols are skipped unless
    strict. Returns the list of names that were bound.
    """
    bound = []
    for name, (restype, argtypes, variadic) in PROTOTYPES.items():
        if names is not None and name not in names:
            continue
        try:
            fn = getattr(lib, name)
        except AttributeError:
            if strict:
                raise
            continue
        fn.restype = restype
        if not variadic:
            fn.argtypes = argtypes
        bound.append(name)
    return bound
''')
    with open(os.path.join(out_dir, "functions.py"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(os.path.join(out_dir, "__init__.py"), "w") as fh:
        fh.write(HEADER.format(version=version) +
                 "from . import constants, types, functions  # noqa: F401\n"
                 "from .functions import bind, PROTOTYPES  # noqa: F401\n")


LOADER_TEMPLATE = r'''"""
Load the FreeRDP libraries from pyfreerdpnative/_libs and attach every prototype
declared in the headers (restype + argtypes), so calls are type-checked by
ctypes and the struct/constant definitions from the header mirror apply.

    from pyfreerdpnative import load
    api = load()                      # finds pyfreerdpnative/_libs automatically
    api.freerdp_get_version_string()  # -> b'{version}'
    ctx = api.freerdp_client_context_new(ctypes.byref(entry))

`api.<name>` resolves the function in whichever library exports it (winpr,
freerdp, freerdp-client, freerdp-server). `api.libs` holds the raw CDLL
handles, `api.types` / `api.constants` the header definitions.
"""
import ctypes
import glob
import os
import platform

from ._core import constants, types
from ._core.functions import bind

# Load order matters: each depends on the ones before it.
LIBRARIES = ("winpr3", "freerdp3", "freerdp-client3", "freerdp-server3")

# Optional extras that some editions ship (media backends etc.). Loaded if
# present so that the core libraries can resolve them; never required.
OPTIONAL = ("winpr-tools3", "rdtk0", "uwac0")


def _ext():
    return {{"Windows": ".dll", "Darwin": ".dylib"}}.get(platform.system(), ".so")


def default_libs_dir():
    """
    Where the FreeRDP libraries live. Precedence:
      1. $PYFREERDP_LIBS - an explicit override always wins
      2. pyfreerdpnative/_libs next to this file (the bundled libraries)
      3. a _libs directory in a parent (source checkouts with a different layout)
    A directory only counts if it actually contains a FreeRDP library, so an
    empty placeholder cannot shadow a real location further down the list.
    """
    def has_libs(d):
        try:
            return any(f.startswith(("libfreerdp3", "freerdp3", "libwinpr3", "winpr3"))
                       for f in os.listdir(d))
        except OSError:
            return False

    env = os.environ.get("PYFREERDP_LIBS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "_libs")]
    for up in (1, 2, 3):
        candidates.append(os.path.normpath(os.path.join(here, *([".."] * up + ["_libs"]))))
    for cand in candidates:
        if has_libs(cand):
            return cand
    return candidates[0]        # bundled location, even if empty (clear error later)

def find_library(libs_dir, stem):
    """First file in libs_dir for a library stem, any platform naming."""
    ext = _ext()
    patterns = [
        os.path.join(libs_dir, "lib" + stem + ext + "*"),   # libfreerdp3.so, .so.3
        os.path.join(libs_dir, "lib" + stem + "*" + ext),   # libfreerdp3.3.dylib
        os.path.join(libs_dir, stem + ext),                 # freerdp3.dll
    ]
    for pat in patterns:
        hits = sorted(p for p in glob.glob(pat) if os.path.isfile(p))
        if hits:
            # prefer the plain / shortest name (libfreerdp3.so over .so.3.31.0)
            hits.sort(key=lambda p: (len(os.path.basename(p)), p))
            return hits[0]
    return None


# --- ELF inspection -------------------------------------------------------
# Android's linker resolves a library's DT_NEEDED entries only against
# already-loaded sonames and its own default search paths - never against the
# directory the library was loaded from. A bundled _libs therefore has to be
# loaded bottom-up: every dependency dlopen'd by absolute path BEFORE the
# library that needs it. Reading DT_SONAME / DT_NEEDED here keeps that order
# correct whatever the build shipped.

def _elf_dynamic(path):
    """(soname, [needed, ...]) from an ELF file; (None, []) if unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (IOError, OSError):
        return None, []
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None, []                       # not ELF (Mach-O, PE, script)
    import struct
    is64 = data[4] == 2 if isinstance(data[4], int) else ord(data[4]) == 2
    little = (data[5] == 1 if isinstance(data[5], int) else ord(data[5]) == 1)
    end = "<" if little else ">"
    try:
        if is64:
            e_phoff, = struct.unpack_from(end + "Q", data, 0x20)
            e_phentsize, e_phnum = struct.unpack_from(end + "HH", data, 0x36)
        else:
            e_phoff, = struct.unpack_from(end + "I", data, 0x1C)
            e_phentsize, e_phnum = struct.unpack_from(end + "HH", data, 0x2A)
        dyn_off = dyn_size = 0
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from(end + "I", data, off)
            if p_type == 2:                   # PT_DYNAMIC
                # p_offset (file offset), NOT p_vaddr: they are equal in many
                # shared objects but not all, and reading the wrong one makes
                # the parse silently yield nothing.
                if is64:
                    dyn_off, = struct.unpack_from(end + "Q", data, off + 0x08)
                    dyn_size, = struct.unpack_from(end + "Q", data, off + 0x20)
                else:
                    dyn_off, = struct.unpack_from(end + "I", data, off + 0x04)
                    dyn_size, = struct.unpack_from(end + "I", data, off + 0x10)
                break
        if not dyn_size:
            return None, []
        entry = 16 if is64 else 8
        fmt = end + ("Q" if is64 else "I")
        strtab = strsz = 0
        raw = []
        for pos in range(dyn_off, dyn_off + dyn_size, entry):
            tag, = struct.unpack_from(fmt, data, pos)
            val, = struct.unpack_from(fmt, data, pos + entry // 2)
            if tag == 0:                      # DT_NULL
                break
            if tag == 5:                      # DT_STRTAB (virtual address)
                strtab = val
            elif tag == 10:                   # DT_STRSZ
                strsz = val
            elif tag in (1, 14):              # DT_NEEDED, DT_SONAME
                raw.append((tag, val))
        if not strtab:
            return None, []
        # translate the strtab virtual address to a file offset via PT_LOAD
        file_off = None
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from(end + "I", data, off)
            if p_type != 1:                   # PT_LOAD
                continue
            if is64:
                p_offset, p_vaddr = struct.unpack_from(end + "QQ", data, off + 0x08)
                p_filesz, = struct.unpack_from(end + "Q", data, off + 0x20)
            else:
                p_offset, p_vaddr = struct.unpack_from(end + "II", data, off + 0x04)
                p_filesz, = struct.unpack_from(end + "I", data, off + 0x10)
            if p_vaddr <= strtab < p_vaddr + p_filesz:
                file_off = p_offset + (strtab - p_vaddr)
                break
        if file_off is None:
            return None, []
        blob = data[file_off:file_off + (strsz or 0x10000)]

        def name_at(idx):
            stop = blob.find(b"\x00", idx)
            return blob[idx:stop if stop >= 0 else None].decode("utf-8", "replace")

        soname, needed = None, []
        for tag, val in raw:
            if tag == 14:
                soname = name_at(val)
            else:
                needed.append(name_at(val))
        return soname, needed
    except (struct.error, IndexError, ValueError):
        return None, []


def _load_order(libs_dir, files):
    """
    Order `files` so that every dependency inside libs_dir is loaded before
    the library needing it. Returns (ordered_paths, info) where info maps a
    path to (soname, [needed, ...]).
    """
    info = {{}}
    by_soname = {{}}
    for path in files:
        soname, needed = _elf_dynamic(path)
        info[path] = (soname, needed)
        by_soname.setdefault(soname or os.path.basename(path), path)
        by_soname.setdefault(os.path.basename(path), path)

    # Fallback rank, used when a file yields no DT_NEEDED (unreadable, or a
    # format this parser does not know): third-party dependencies first, then
    # winpr, freerdp, and the libraries built on top. Without this a failed
    # parse degrades to directory order, which on Android loads
    # libfreerdp-client3.so before libfreerdp3.so and fails.
    def rank(path):
        base = os.path.basename(path)
        if base.startswith(("libwinpr3", "libwinpr.")):
            return 1
        if base.startswith("libfreerdp3"):
            return 2
        if base.startswith(("libfreerdp-client", "libfreerdp-server", "libuwac", "librdtk",
                            "libwinpr-tools")):
            return 3
        return 0                                   # ssl, crypto, cjson, c++_shared, ...

    files = sorted(files, key=lambda p: (rank(p), os.path.basename(p)))

    ordered, seen, visiting = [], set(), set()

    def visit(path):
        if path in seen or path in visiting:
            return
        visiting.add(path)
        for dep in info.get(path, (None, []))[1]:
            target = by_soname.get(dep)
            if target and target != path:
                visit(target)
        visiting.discard(path)
        seen.add(path)
        ordered.append(path)

    for path in files:
        visit(path)
    return ordered, info


class FreeRDP(object):
    """Bound libraries with header-derived prototypes attached."""

    def __init__(self, libs_dir=None, strict=False):
        self.libs_dir = os.path.abspath(libs_dir or default_libs_dir())
        if not os.path.isdir(self.libs_dir):
            raise OSError("FreeRDP libraries directory not found: {{0}}".format(
                self.libs_dir))
        self.types = types
        self.constants = constants
        self.libs = {{}}
        self.bound = {{}}
        self._lookup = {{}}

        if platform.system() == "Windows" and hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(self.libs_dir)
        mode = getattr(ctypes, "RTLD_GLOBAL", 0)

        # Core libraries first, in dependency order. dlsym() on a handle also
        # searches that library's dependencies, so whichever library is
        # bound FIRST claims a symbol - binding winpr before freerdp before
        # the client/server libs (and the optional extras last) attributes
        # every function to the library that really exports it.
        wanted = {{}}
        for stem in LIBRARIES + OPTIONAL:
            path = find_library(self.libs_dir, stem)
            if path is None:
                if stem in LIBRARIES:
                    raise OSError("{{0}} not found in {{1}} (files: {{2}})".format(
                        stem, self.libs_dir,
                        ", ".join(sorted(os.listdir(self.libs_dir))[:12])))
                continue
            wanted[os.path.realpath(path)] = stem

        # Pre-load everything in _libs bottom-up, by absolute path. On Linux
        # and macOS the rpath ($ORIGIN / @loader_path) would find siblings on
        # its own, but Android's linker never searches the directory a
        # library came from: a dependency is only resolved if it is already
        # loaded under that soname. Loading dependencies first is what makes
        # the bundled _libs work there (Termux included).
        try:
            everything = [os.path.join(self.libs_dir, f)
                          for f in sorted(os.listdir(self.libs_dir))]
        except OSError:
            everything = []
        everything = [p for p in everything if os.path.isfile(p)]
        ordered, elf_info = _load_order(self.libs_dir, everything)
        self._elf = elf_info
        preloaded = {{}}
        for path in ordered:
            try:
                preloaded[os.path.realpath(path)] = ctypes.CDLL(path, mode=mode)
            except OSError as exc:
                if os.path.realpath(path) in wanted:
                    # Py2-compatible syntax: no `raise ... from`
                    raise OSError(self._explain(path, exc, elf_info, preloaded))  # noqa: B904

        # Bind in LIBRARIES + OPTIONAL order, NOT in preload order: dlsym on a
        # handle also searches that library's dependencies, so whichever
        # library is bound first claims a shared symbol. winpr before freerdp
        # before the client/server libs keeps `library_of()` honest.
        by_stem = dict((v, k) for k, v in wanted.items())
        for stem in LIBRARIES + OPTIONAL:
            path = by_stem.get(stem)
            if path is None:
                continue
            lib = preloaded.get(path)
            if lib is None:
                continue
            self.libs[stem] = lib
            names = bind(lib, strict=strict)
            self.bound[stem] = names
            for n in names:
                self._lookup.setdefault(n, stem)
        missing = [s for s in LIBRARIES if s not in self.libs]
        if missing:
            raise OSError("could not load {{0}} from {{1}}".format(
                ", ".join(missing), self.libs_dir))

    def _explain(self, path, exc, elf_info, loaded):
        """Turn a dlopen failure into something actionable."""
        soname, needed = elf_info.get(path, (None, []))
        present = set()
        for other, (oname, _n) in elf_info.items():
            present.add(oname or os.path.basename(other))
            present.add(os.path.basename(other))
        unmet = [d for d in needed if d not in present]
        lines = ["failed to load {{0}}: {{1}}".format(os.path.basename(path), exc),
                 "  directory: {{0}}".format(self.libs_dir),
                 "  soname:    {{0}}".format(soname or "(none)")]
        if needed:
            lines.append("  needs:     {{0}}".format(", ".join(needed)))
        if unmet:
            lines.append("  NOT in this directory: {{0}}".format(", ".join(unmet)))
            lines.append("  -> these must be provided by the system, or bundled "
                         "into _libs next to the others")
        else:
            lines.append("  every dependency is present in the directory; the "
                         "failure is the loader's, e.g. an ABI or page-size "
                         "mismatch (Android 15+ needs 16 KB-aligned libraries)")
        return "\n".join(lines)

    # --- attribute access -----------------------------------------------
    def __getattr__(self, name):
        stem = self._lookup.get(name)
        if stem is None:
            # not a header prototype: try each library raw (e.g. non-exported
            # symbols present on this platform) so callers still get something
            for lib in self.libs.values():
                try:
                    return getattr(lib, name)
                except AttributeError:
                    pass
            raise AttributeError("{{0}} is not exported by any loaded FreeRDP "
                                 "library".format(name))
        return getattr(self.libs[stem], name)

    def has(self, name):
        return name in self._lookup

    def library_of(self, name):
        """Which library exports a function ('freerdp3', 'winpr3', ...)."""
        return self._lookup.get(name)

    def version(self):
        return self.freerdp_get_version_string().decode("ascii", "replace")

    def __repr__(self):
        return "<FreeRDP {{0}} from {{1}}: {{2}} prototypes bound>".format(
            self.version(), self.libs_dir, len(self._lookup))


_default = None


def load(libs_dir=None, strict=False):
    """Load (once) and return the bound FreeRDP libraries."""
    global _default
    if libs_dir is None and _default is not None:
        return _default
    api = FreeRDP(libs_dir, strict=strict)
    if libs_dir is None:
        _default = api
    return api
'''


def emit_mirror(em, macros, const_values, out_dir, version, macro_files):
    """
    One .py per .h, same name, same subfolder:

        freerdp/codec/color.h  ->  freerdp/codec/color.py

    Constants are written inline with their values (they have no ordering
    problem). Types and functions live in the consistently-ordered `_core`
    package and are re-exported by name, so `from ...freerdp.freerdp import
    rdpContext` works and every module is importable on its own.
    """
    by_header = {}

    def add(h, kind, name):
        by_header.setdefault(h, {"const": [], "type": [], "func": []})[kind].append(name)

    for name, h in macro_files.items():
        if name in const_values:
            add(h, "const", name)
    for name, h in em.origin["enum"].items():
        add(h, "const", name)
    for name, hs in em.origin["typedef"].items():
        for h in hs:
            add(h, "type", name)
    for tag, h in em.origin["struct"].items():
        add(h, "type", em.struct_class_name(tag))
        # also export the typedef alias(es) of this struct where the body is
        # defined: `typedef struct rdp_context rdpContext;` sits in types.h,
        # the layout in freerdp.h - readers of freerdp.h expect `rdpContext`.
        cls = em.struct_class_name(tag)
        for alias, expr in em.typedefs.items():
            if expr == cls:
                add(h, "type", alias)
    for name, h in em.origin["func"].items():
        add(h, "func", name)

    # Only headers that actually declare something get a module; a header
    # that merely includes other headers has nothing to mirror.
    by_header = {h: v for h, v in by_header.items()
                 if v["const"] or v["type"] or v["func"]}

    # directories in the mirror (a header named like a directory, e.g.
    # freerdp/client.h next to freerdp/client/, becomes that package's
    # __init__.py so both remain importable: `from x.freerdp import client`)
    dirs = set()
    for h in by_header:
        parts = h.replace("-", "_").split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))

    by_header_inits = {h[:-2].replace("-", "_") for h in by_header
                       if h.endswith(".h") and h[:-2].replace("-", "_") in dirs}
    mirror_root = out_dir
    for h in sorted(by_header):
        rel = h[:-2] if h.endswith(".h") else h
        rel = rel.replace("-", "_")                    # rdpecam-enumerator.h
        as_package_init = rel in dirs
        parts = rel.split("/")
        if as_package_init:
            parts = parts + ["__init__"]
        mod_dir = os.path.join(mirror_root, *parts[:-1])
        if not os.path.isdir(mod_dir):
            os.makedirs(mod_dir)
        # package __init__ for every directory level
        acc = mirror_root
        for part in parts[:-1]:
            acc = os.path.join(acc, part)
            init = os.path.join(acc, "__init__.py")
            if not os.path.exists(init) and "/".join(parts[:parts.index(part) + 1]) not in by_header_inits:
                with open(init, "w") as fh:
                    fh.write(HEADER.format(version=version))
        # freerdp/codec/color.py sits two packages below `generated`:
        # one dot = its own package (codec), one per level up -> "..." + _core.
        # An __init__.py resolves relative imports exactly like a module
        # inside its package, so parts (incl. "__init__") gives the depth.
        depth = len(parts) - 1
        core = "." * (depth + 1) + "_core"
        items = by_header[h]
        lines = ['"""', "Mirror of <{0}> (FreeRDP {1}).".format(h, version),
                 "", "Generated by scripts/gen_bindings.py - do not edit.",
                 '"""', "import ctypes  # noqa: F401", ""]
        if items["type"] or items["func"]:
            lines.append("from {0} import types as _types, functions as _functions  # noqa: E402".format(core))
            lines.append("")
        exported = []
        if items["const"]:
            lines.append("# --- #defines and enum members declared in this header ---")
            for name in sorted(set(items["const"])):
                v = const_values[name]
                # hex for anything that is plausibly a flag/format/key id
                text = hex(v) if isinstance(v, int) and v > 9 else repr(v)
                lines.append("{0} = {1}".format(name, text))
                exported.append(name)
            lines.append("")
        if items["type"]:
            lines.append("# --- types declared in this header ---")
            for name in sorted(set(items["type"])):
                lines.append("{0} = _types.{0}".format(name))
                exported.append(name)
            lines.append("")
        if items["func"]:
            lines.append("# --- functions declared in this header ---")
            lines.append("FUNCTIONS = (")
            for name in sorted(set(items["func"])):
                res, args, va = em.functions[name]
                lines.append("    {0!r},  # {1} ({2}{3})".format(
                    name, res or "void", ", ".join(args), ", ..." if va else ""))
            lines.append(")")
            lines.append("")
            lines.append("")
            lines.append("def bind(lib, strict=False):")
            lines.append('    """Attach restype/argtypes of this header\'s functions to a loaded library."""')
            lines.append("    return _functions.bind(lib, names=FUNCTIONS, strict=strict)")
            lines.append("")
            exported += ["FUNCTIONS", "bind"]
        lines.append("__all__ = {0!r}".format(sorted(set(exported))))
        with open(os.path.join(mod_dir, parts[-1] + ".py"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
    return len(by_header)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

# Public headers to feed the parser. Everything they include comes along.
ROOTS = [
    "freerdp/freerdp.h", "freerdp/client.h", "freerdp/peer.h",
    "freerdp/listener.h", "freerdp/settings.h", "freerdp/input.h",
    "freerdp/update.h", "freerdp/codecs.h", "freerdp/channels/channels.h",
    "freerdp/gdi/gdi.h", "freerdp/gdi/gfx.h", "freerdp/client/channels.h",
    "freerdp/client/cliprdr.h", "freerdp/client/rdpgfx.h",
    "freerdp/client/rdpei.h", "freerdp/client/rail.h", "freerdp/client/disp.h",
    "freerdp/client/rdpsnd.h", "freerdp/client/audin.h", "freerdp/client/drdynvc.h",
    "freerdp/client/encomsp.h", "freerdp/client/remdesk.h", "freerdp/client/rdpdr.h",
    "freerdp/server/channels.h", "freerdp/server/cliprdr.h",
    "freerdp/server/rdpgfx.h", "freerdp/server/rdpei.h", "freerdp/server/rail.h",
    "freerdp/server/disp.h", "freerdp/server/rdpsnd.h", "freerdp/server/audin.h",
    "freerdp/server/drdynvc.h", "freerdp/server/encomsp.h",
    "freerdp/server/remdesk.h", "freerdp/server/rdpdr.h", "freerdp/server/echo.h",
    "freerdp/server/location.h", "freerdp/server/ainput.h",
    "freerdp/server/telemetry.h", "freerdp/server/rdpemsc.h",
    "freerdp/server/rdpecam.h", "freerdp/server/rdpecam-enumerator.h",
    "freerdp/server/shadow.h", "freerdp/server/proxy/proxy_context.h",
    "freerdp/server/proxy/proxy_modules_api.h", "freerdp/server/proxy/proxy_server.h",
    "freerdp/codec/h264.h", "freerdp/codec/color.h", "freerdp/codec/rfx.h",
    "freerdp/codec/progressive.h", "freerdp/codec/planar.h",
    "freerdp/codec/clear.h", "freerdp/codec/dsp.h", "freerdp/codec/audio.h",
    "freerdp/utils/passphrase.h", "freerdp/utils/signal.h",
    "freerdp/crypto/certificate.h", "freerdp/crypto/privatekey.h",
    "freerdp/locale/keyboard.h", "freerdp/scancode.h", "freerdp/error.h",
    "freerdp/version.h", "freerdp/streamdump.h", "freerdp/assistance.h",
    "freerdp/redirection.h", "freerdp/session.h", "freerdp/heartbeat.h",
    "winpr/winpr.h", "winpr/wtypes.h", "winpr/stream.h", "winpr/synch.h",
    "winpr/thread.h", "winpr/sspi.h", "winpr/crypto.h", "winpr/collections.h",
    "winpr/wlog.h", "winpr/path.h", "winpr/file.h", "winpr/library.h",
    "winpr/error.h", "winpr/string.h", "winpr/sysinfo.h", "winpr/clipboard.h",
    "winpr/input.h", "winpr/handle.h", "winpr/environment.h", "winpr/image.h",
    "winpr/json.h", "winpr/ini.h", "winpr/print.h", "winpr/registry.h",
    "winpr/smartcard.h", "winpr/timezone.h", "winpr/wtsapi.h", "winpr/ncrypt.h",
    "winpr/schannel.h", "winpr/ssl.h", "winpr/tools/makecert.h",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--include", action="append", required=True,
                    help="include directory (repeat); must cover freerdp/, "
                         "winpr/ and the generated config/version headers")
    ap.add_argument("--out", required=True, help="output package directory")
    ap.add_argument("--version", default="", help="label for the file headers")
    ap.add_argument("--fake-libc", metavar="DIR",
                    help="pycparser's utils/fake_libc_include directory "
                         "(auto-fetched into ~/.cache when omitted)")
    ap.add_argument("--define", action="append", default=[],
                    help="extra -D for the preprocessor")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if not HAVE_PYCPARSER:
        sys.exit("pycparser is required to generate bindings: pip install pycparser")

    includes = [os.path.abspath(i) for i in args.include]
    fake = find_fake_libc(args.fake_libc)
    if not fake:
        print("warning: pycparser fake_libc_include not found; system headers "
              "may not parse", file=sys.stderr)

    # One big translation unit that includes EVERY public header found under
    # freerdp/ and winpr/ in the include dirs (ROOTS first for a sensible
    # order, then everything else), so no header is left unmirrored because
    # nothing happened to include it.
    all_headers = []
    for inc in includes:
        for top in ("freerdp", "winpr"):
            for root, dirs, files in os.walk(os.path.join(inc, top)):
                dirs.sort()          # os.walk order is filesystem-dependent; make it stable
                for fn in sorted(files):
                    if fn.endswith(".h"):
                        rel = os.path.relpath(os.path.join(root, fn), inc).replace(os.sep, "/")
                        if rel not in all_headers and not EXCLUDE_HEADERS.search(rel):
                            all_headers.append(rel)
    separate = {h for group in SEPARATE_TUS for h in group}
    ordered = [r for r in ROOTS if r in all_headers and r not in separate] + \
              [h for h in all_headers if h not in ROOTS and h not in separate]
    tus = [ordered] + [[h for h in group if h in all_headers] for group in SEPARATE_TUS]
    tus = [t for t in tus if t]

    em = Emitter([os.path.abspath(i) for i in includes])
    em.include_dirs = [os.path.abspath(i) for i in includes]
    macros = {}
    macro_files = {}
    parser = c_parser.CParser()
    scratch = tempfile.mkdtemp(prefix="gen_bindings_")
    for idx, headers in enumerate(tus):
        tu = PRELUDE + "\n".join("#include <{0}>".format(r) for r in headers)
        # scratch files live in a temp dir, never inside the output package
        # (where they would be picked up as package files or shadow modules)
        tmp = os.path.join(scratch, "_all_roots_{0}.c".format(idx))
        with open(tmp, "w") as fh:
            fh.write(tu + "\n")
        try:
            src = preprocess(tmp, includes, fake, args.define)
            m = constants_from_macros(tmp, includes, fake)
            for name, text in expand_macros(tmp, includes, fake, sorted(m)).items():
                m[name] = text
            origins = macro_origins(tmp, includes, fake)
            for k, v in m.items():
                if ENVIRONMENT_MACROS.match(k) or ENVIRONMENT_HEADERS.search(origins.get(k, "")):
                    continue
                macros.setdefault(k, v)
            for k, v in origins.items():
                if not (ENVIRONMENT_MACROS.match(k) or ENVIRONMENT_HEADERS.search(v)):
                    macro_files.setdefault(k, v)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            ast = parser.parse(src, filename="<freerdp>")
        except Exception as e:                       # show context for parse errors
            msg = str(e)
            mm = re.search(r"([^:\s]+):(\d+):(\d+)", msg)
            if mm:
                fname, ln = mm.group(1), int(mm.group(2))
                cur_file, cur_line, ctx = None, 0, []
                for raw in src.splitlines():
                    m2 = re.match(r'# (\d+) "([^"]*)"', raw)
                    if m2:
                        cur_line, cur_file = int(m2.group(1)) - 1, m2.group(2)
                        continue
                    cur_line += 1
                    if cur_file and cur_file.endswith(os.path.basename(fname)) \
                            and abs(cur_line - ln) <= 3:
                        ctx.append("{0:>5}: {1}".format(cur_line, raw))
                sys.exit("parse error: {0}\n--- {1} around line {2} ---\n{3}".format(
                    msg, fname, ln, "\n".join(ctx)))
            raise
        # int constants must be known before the AST walk (array dims etc.)
        em.int_constants = {}
        for name, value in macros.items():
            lit = eval_macro(value, em.int_constants)
            if lit is not None:
                try:
                    v = eval(lit)
                    if isinstance(v, int):
                        em.int_constants[name] = v
                except Exception:
                    pass
        em.visit(ast)

    version = args.version
    if not version:
        v = macros.get("FREERDP_VERSION_FULL", "").strip('"')
        version = v or "unknown"
    core_dir = os.path.join(args.out, "_core")
    emit(em, macros, core_dir, version)

    # values of every constant the core resolved (for the inline mirror copies)
    const_values = {}
    ns = {}
    with open(os.path.join(core_dir, "constants.py")) as fh:
        exec(fh.read(), ns)                          # generated by us, trusted
    for k, v in ns.items():
        if not k.startswith("__") and isinstance(v, (int, str)):
            const_values[k] = v

    n_headers = emit_mirror(em, macros, const_values, args.out, version, macro_files)
    with open(os.path.join(args.out, "_loader.py"), "w") as fh:
        fh.write(LOADER_TEMPLATE.format(version=version))
    with open(os.path.join(args.out, "__init__.py"), "w") as fh:
        fh.write(HEADER.format(version=version) +
                 "from ._core import constants, types, functions  # noqa: F401\n"
                 "from ._core.functions import bind, PROTOTYPES  # noqa: F401\n"
                 "from ._loader import load, FreeRDP, find_library, default_libs_dir  # noqa: F401\n"
                 "# Header mirror: pyfreerdpnative.freerdp.freerdp, pyfreerdpnative.winpr.sspi, ... (one module per .h)\n")
    print("  mirror    : {0} header modules (one .py per .h, same folders)".format(n_headers))
    shutil.rmtree(scratch, ignore_errors=True)

    print("generated into {0}:".format(args.out))
    print("  constants : {0} #defines + {1} enum members".format(
        sum(1 for m in macros if eval_macro(macros[m], em.int_constants)),
        sum(len(m) for _n, m in em.enums)))
    print("  types     : {0} typedefs, {1} structs/unions ({2} with layout)".format(
        len(em.typedefs), len(em.structs),
        sum(1 for k in em.structs if em.structs[k][1])))
    print("  functions : {0} prototypes".format(len(em.functions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
