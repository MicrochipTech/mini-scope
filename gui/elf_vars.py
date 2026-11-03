"""elf_vars.py - resolves C expressions such as ``motor.run`` or ``phase[1].current`` to address,
width and type, from the DWARF data of an ELF file (pyelftools) or from a plain name table.

    t = VarTable.from_elf('test.X.production.elf')     # or VarTable.from_symbols({'x': (0x1000, 2)})
    v = t.lookup('phase[1].current')                   # Var(address=..., width=2, type='s16', ...)
    t.names()                                          # every name lookup() accepts
    t.read_string('tune_build')                        # a const char array, read from the ELF (flash), not from RAM

A plain number ('0x1100', '4352') is an address and always works; it has no width and no type.
Every other problem raises VarError(rule, message), rule is a short identifier:
  not_found  needs_member  needs_index  index_range  pointer  bitfield  width  ambiguous
  no_debug   not_ram       syntax       not_struct   not_array  bad_address  unsupported
  not_string (read_string: not a char array, not in the file, or no end of text)
No import of scope_gui: the type strings are the keys of its TYPES table (s8 u8 s16 u16 s32 u32 f32).
"""
import difflib
import io
import re
from collections import namedtuple

from elftools.elf.elffile import ELFFile

RAM_END = 0x8000        # data space below this address is RAM or SFR, above it flash (const data)
MAX_PER_VAR = 64        # names() expands at most this many names per top-level variable
DEFAULT_TYPE = {1: 's8', 2: 's16', 4: 's32'}       # same as scope_gui.DEFAULT_TYPE

# type is None and width is None for a plain address; is_volatile / is_const are None when unknown (name table)
Var = namedtuple('Var', 'name address width type ctype is_volatile is_const in_ram')


class VarError(Exception):
    """rule: short identifier (see module doc), message: text for the user, suggestions: close names."""

    def __init__(self, rule, message, suggestions=()):
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.suggestions = list(suggestions)


def _did_you_mean(sugg):
    return f' - did you mean {", ".join(sugg)}?' if sugg else ''


def _close(word, candidates, n=3):
    """Closest candidates, case blind, best first."""
    low = {}
    for c in candidates:
        low.setdefault(c.lower(), c)
    return [low[m] for m in difflib.get_close_matches(word.lower(), list(low), n=n, cutoff=0.6)]


def _uleb(data):
    v = shift = 0
    for b in data:
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    return v


def _attr(die, name, default=None):
    a = die.attributes.get(name)
    return default if a is None else a.value


def _text(v):
    return v.decode('utf-8', 'replace') if isinstance(v, bytes) else v


def _base(path):
    return re.split(r'[\\/]', path)[-1]


class _Type:
    """A type with typedef and qualifiers stripped. kind: scalar, struct, array, ptr, other."""

    def __init__(self, kind, size=0, scalar=None, ctype='', members=(), elem=None, dims=()):
        self.kind, self.size, self.scalar, self.ctype = kind, size, scalar, ctype
        self.members, self.elem, self.dims = list(members), elem, tuple(dims)
        # members: (name, offset, type die, bit_size or None, volatile, const, storage bytes)


def _scalar_name(enc, size):
    if size not in (1, 2, 4):
        return None
    if enc == 4:                        # DW_ATE_float
        return 'f32' if size == 4 else None
    if enc in (5, 6):                   # signed, signed_char
        return f's{8 * size}'
    return f'u{8 * size}'               # unsigned, unsigned_char, boolean


class VarTable:
    """Name -> variable, from an ELF with debug information or from a name table."""

    def __init__(self):
        self.has_debug = False
        self._vars = {}         # name -> [(file, address, die)]; ELF
        self._flat = {}         # name -> (address, width, type); name table
        self._strings = {}      # name -> text; name table
        self._elf = None
        self._types = {}        # die offset -> _Type
        self._names = None

    # -- construction -------------------------------------------------------------------------

    @classmethod
    def from_symbols(cls, symbols, types=None, strings=None):
        """symbols: name -> (address, size), like scope_gui.load_symbols. types: name -> 's16' etc.,
        a missing entry gets the signed type of its width. strings: name -> text of const char arrays
        for read_string(); if None, the attribute `strings` of symbols is used, if it has one."""
        t = cls()
        types = types or {}
        t._strings = dict(strings if strings is not None else getattr(symbols, 'strings', {}))
        for n, (a, s) in symbols.items():
            t._flat[n] = (a, s, types.get(n) or DEFAULT_TYPE.get(s))
        return t

    @classmethod
    def from_elf(cls, path):
        t = cls()
        with open(path, 'rb') as f:
            elf = ELFFile(io.BytesIO(f.read()))
        t._elf = elf
        if not elf.has_dwarf_info():
            return t
        t._dwarf = elf.get_dwarf_info()
        for cu in t._dwarf.iter_CUs():
            top = cu.get_top_DIE()
            cname = _base(_text(_attr(top, 'DW_AT_name', b'')))
            asize = cu['address_size']
            for die in top.iter_children():
                if die.tag != 'DW_TAG_variable' or 'DW_AT_declaration' in die.attributes:
                    continue
                spec = die.get_DIE_from_attribute('DW_AT_specification') if 'DW_AT_specification' in die.attributes else die
                name = _attr(spec, 'DW_AT_name') or _attr(die, 'DW_AT_name')
                loc = _attr(die, 'DW_AT_location')
                if (name is None or not isinstance(loc, list) or len(loc) != 1 + asize or loc[0] != 3
                        or ('DW_AT_type' not in die.attributes and 'DW_AT_type' not in spec.attributes)):
                    continue            # no static address (optimised away) or no type
                tdie = (die if 'DW_AT_type' in die.attributes else spec).get_DIE_from_attribute('DW_AT_type')
                addr = int.from_bytes(bytes(loc[1:]), 'little')
                lst = t._vars.setdefault(_text(name), [])
                if all(e[1] != addr for e in lst):
                    lst.append((cname, addr, tdie))
        t.has_debug = bool(t._vars)
        return t

    # -- types --------------------------------------------------------------------------------

    @staticmethod
    def _target(die):
        return die.get_DIE_from_attribute('DW_AT_type') if 'DW_AT_type' in die.attributes else None

    def _strip(self, die):
        """Follow typedef / const / volatile. Returns (die or None for void, volatile, const, first typedef name)."""
        vol = const = False
        tname = None
        while die is not None:
            tag = die.tag
            if tag == 'DW_TAG_volatile_type':
                vol = True
            elif tag == 'DW_TAG_const_type':
                const = True
            elif tag == 'DW_TAG_typedef':
                tname = tname or _text(_attr(die, 'DW_AT_name', b''))
            elif tag not in ('DW_TAG_restrict_type', 'DW_TAG_atomic_type'):
                break
            die = self._target(die)
        return die, vol, const, tname

    def _type(self, die):
        """-> (_Type, volatile, const) of a type DIE, qualifiers from the whole chain."""
        d, vol, const, tname = self._strip(die)
        if d is None:
            return _Type('other', ctype='void'), vol, const
        t = self._types.get(d.offset)
        if t is None:
            t = self._types[d.offset] = self._build(d)
        if tname and t.ctype != tname:
            t = _Type(t.kind, t.size, t.scalar, tname, t.members, t.elem, t.dims)
        return t, vol, const

    def _build(self, d):
        tag, size = d.tag, _attr(d, 'DW_AT_byte_size', 0)
        name = _text(_attr(d, 'DW_AT_name', b''))
        if tag == 'DW_TAG_base_type':
            return _Type('scalar', size, _scalar_name(_attr(d, 'DW_AT_encoding'), size), name)
        if tag == 'DW_TAG_enumeration_type':
            neg = any(_attr(c, 'DW_AT_const_value', 0) < 0 for c in d.iter_children() if c.tag == 'DW_TAG_enumerator')
            return _Type('scalar', size, _scalar_name(5 if neg else 7, size), 'enum ' + name if name else 'enum')
        if tag == 'DW_TAG_pointer_type':
            return _Type('ptr', size, ctype='pointer')
        if tag in ('DW_TAG_structure_type', 'DW_TAG_union_type'):
            return _Type('struct', size, ctype=('union ' if tag == 'DW_TAG_union_type' else 'struct ') + name,
                         members=self._members(d, 0, False, False))
        if tag == 'DW_TAG_array_type':
            dims = []
            for c in d.iter_children():
                if c.tag == 'DW_TAG_subrange_type':
                    if 'DW_AT_upper_bound' in c.attributes and isinstance(c.attributes['DW_AT_upper_bound'].value, int):
                        dims.append(c.attributes['DW_AT_upper_bound'].value + 1)
                    elif 'DW_AT_count' in c.attributes:
                        dims.append(c.attributes['DW_AT_count'].value)
                    else:
                        dims.append(None)
            return _Type('array', size, elem=self._target(d), dims=dims or [None], ctype='array')
        return _Type('other', size, ctype=name or tag)

    def _members(self, d, base, vol, const):
        out = []
        for m in d.iter_children():
            if m.tag != 'DW_TAG_member':
                continue
            loc = _attr(m, 'DW_AT_data_member_location', 0)
            off = base + (_uleb(loc[1:]) if isinstance(loc, list) and loc and loc[0] == 0x23 else
                          loc if isinstance(loc, int) else 0)
            mname = _attr(m, 'DW_AT_name')
            tdie = self._target(m)
            if mname is None:           # anonymous struct or union: its members belong to the parent
                sd, v, c, _ = self._strip(tdie)
                if sd is not None and sd.tag in ('DW_TAG_structure_type', 'DW_TAG_union_type'):
                    out += self._members(sd, off, vol or v, const or c)
                continue
            out.append((_text(mname), off, tdie, _attr(m, 'DW_AT_bit_size'), vol, const, _attr(m, 'DW_AT_byte_size')))
        return out

    # -- lookup -------------------------------------------------------------------------------

    def lookup(self, name, require_ram=True):
        """-> Var. require_ram=False returns a variable in flash (in_ram False) instead of raising not_ram."""
        text = name.strip()
        try:
            addr = int(text, 0)
        except ValueError:
            addr = None
        if addr is not None:
            if not 0 <= addr <= 0xFFFF:
                raise VarError('bad_address', f'address {text} is outside 0 ... 0xFFFF')
            return Var(text, addr, None, None, None, None, None, addr < RAM_END)
        if self._flat or not self._vars:
            return self._lookup_flat(text, require_ram)
        return self._lookup_elf(text, require_ram)

    def _lookup_flat(self, text, require_ram):
        if not self.has_debug and not self._flat:
            raise VarError('no_debug', f'cannot resolve {text}: the ELF has no debug information, '
                                       'build with -g (and without stripping), or give an address')
        e = self._flat.get(text)
        if e is None:
            sugg = _close(text, self._flat)
            raise VarError('not_found', f'{text} not found' + _did_you_mean(sugg), sugg)
        addr, size, typ = e
        if size not in (1, 2, 4):
            raise VarError('width', f'{text} is {size} bytes wide, only 1, 2 or 4 bytes can be read')
        if require_ram and addr + size > RAM_END:
            raise VarError('not_ram', f'{text} is at 0x{addr:04X}, not in RAM (const data lives in flash)')
        return Var(text, addr, size, typ, None, None, None, addr + size <= RAM_END)

    _HEAD = re.compile(r'\s*(?:([\w.\-]+):\s*)?([A-Za-z_]\w*)')
    _STEP = re.compile(r'\s*(?:\.\s*([A-Za-z_]\w*)|(->)|\[\s*(0[xX][0-9a-fA-F]+|\d+)\s*\])')

    def _lookup_elf(self, text, require_ram):
        m = self._HEAD.match(text)
        if not m:
            raise VarError('syntax', f'{text!r} is not a C variable name or an address')
        qual, base = m.group(1), m.group(2)
        cands = self._vars.get(base)
        if cands and qual:
            cands = [c for c in cands if c[0] == qual]
        if not cands:
            return self._not_found(text, m, base, qual)
        if len(cands) > 1:
            files = ', '.join(f'{c[0]}:{base}' for c in cands)
            raise VarError('ambiguous', f'{base} exists {len(cands)} times (static in several files); '
                                        f'name the file: {files}')
        fname, addr, tdie = cands[0]
        typ, vol, const = self._type(tdie)
        shown = f'{fname}:{base}' if len(self._vars[base]) > 1 else base
        pos = m.end()
        bit = None                      # (storage address, bytes) when the last step was a bit field
        while pos < len(text):
            s = self._STEP.match(text, pos)
            if not s:
                raise VarError('syntax', f'cannot parse {text[pos:].strip()!r} after {shown}')
            pos = s.end()
            member, arrow, index = s.groups()
            if arrow or typ.kind == 'ptr':
                raise VarError('pointer', f'{shown} is a pointer, neither -> nor . can follow it: the target address '
                                          'is not known at build time; use the target variable by its own name or an address')
            if member is not None:
                if typ.kind != 'struct':
                    raise VarError('not_struct', f'{shown} is a {typ.ctype or typ.kind}, it has no member {member}')
                hit = [x for x in typ.members if x[0] == member]
                if not hit:
                    sugg = [shown + '.' + c for c in _close(member, [x[0] for x in typ.members])]
                    raise VarError('not_found', f'{shown} has no member {member}' + _did_you_mean(sugg), sugg)
                _, off, mdie, bits, mv, mc, mbytes = hit[0]
                vol, const = vol or mv, const or mc
                t2, v2, c2 = self._type(mdie)
                vol, const = vol or v2, const or c2
                shown += '.' + member
                addr += off
                typ = t2
                bit = (addr, mbytes or typ.size, bits) if bits is not None else None
            else:
                if typ.kind != 'array':
                    raise VarError('not_array', f'{shown} is a {typ.ctype or typ.kind}, it is not an array')
                i = int(index, 0)
                n = typ.dims[0]
                if n is not None and i >= n:
                    raise VarError('index_range', f'{shown}[{i}] is out of range, {shown} has {n} elements (0 ... {n - 1})')
                et, v2, c2 = self._type(typ.elem)
                vol, const = vol or v2, const or c2
                inner = typ.dims[1:]
                if inner:               # multi dimensional: peel one dimension
                    esize = et.size * _prod(inner)
                    typ = _Type('array', esize, elem=typ.elem, dims=inner, ctype='array')
                else:
                    esize = et.size
                    typ = et
                addr += i * esize
                shown += f'[{i}]'
        return self._finish(shown, addr, typ, vol, const, bit, require_ram)

    def _finish(self, shown, addr, typ, vol, const, bit, require_ram):
        if bit is not None:
            a, nbytes, bits = bit
            raise VarError('bitfield', f'{shown} is a bit field of {bits} bits; read the whole word '
                                       f'(0x{a:04X}, {nbytes} bytes) and mask it')
        if typ.kind == 'struct':
            names = [x[0] for x in typ.members][:6]
            raise VarError('needs_member', f'{shown} is a {typ.ctype}, name a member: '
                                           + ', '.join(f'{shown}.{n}' for n in names) + (' ...' if len(typ.members) > 6 else ''))
        if typ.kind == 'array':
            n = typ.dims[0]
            raise VarError('needs_index', f'{shown} is an array' + (f' of {n}' if n else '') + f', name an element: {shown}[0]')
        if typ.kind == 'ptr':
            raise VarError('pointer', f'{shown} is a pointer; use the target variable by its own name or an address')
        if typ.kind != 'scalar':
            raise VarError('unsupported', f'{shown} has the type {typ.ctype}, which cannot be read')
        if typ.scalar is None:
            raise VarError('width', f'{shown} is {typ.size} bytes wide ({typ.ctype}), only 1, 2 or 4 byte '
                                    'integers and a 4 byte float can be read')
        in_ram = addr + typ.size <= RAM_END
        if require_ram and not in_ram:
            raise VarError('not_ram', f'{shown} is at 0x{addr:04X}, not in RAM (const data lives in flash)')
        return Var(shown, addr, typ.size, typ.scalar, typ.ctype, vol, const, in_ram)

    def _not_found(self, text, m, base, qual):
        if not self.has_debug:
            raise VarError('no_debug', f'cannot resolve {text}: the ELF has no debug information, '
                                       'build with -g (and without stripping), or give an address')
        rest = text[m.end():]
        sugg = []
        for c in _close(base, self._top_names()):
            try:
                self.lookup(c + rest)
                sugg.append(c + rest)
            except VarError:
                pass
        if not sugg:
            sugg = _close(base, self._top_names())
        raise VarError('not_found', f'{(qual + ":" if qual else "") + base} not found' + _did_you_mean(sugg), sugg)

    def _top_names(self):
        out = []
        for n, lst in self._vars.items():
            out += [f'{c[0]}:{n}' for c in lst] if len(lst) > 1 else [n]
        return out

    # -- strings ------------------------------------------------------------------------------

    def read_string(self, name):
        """The text of a const char array such as tune_build, from the ELF file (the flash image), not from the
        target. Raises VarError: not_found, ambiguous, no_debug, or not_string.

        How the text lies in an ELF of a dsPIC (XC-DSC 3.30, dsPIC33CK64MC103): const data sits in the section
        .const, with the address it has in data space (program space visibility window, 0x8000 and up). The file
        holds one 24 bit program word as 4 bytes: the two low bytes are two data bytes, the other two are zero
        (high byte and phantom byte). So the data byte at address a is file byte 4 * ((a - addr) // 2) + (a - addr) % 2
        of the section. The same layout holds for a section at a program address (below 0x8000)."""
        if self._flat or not self._vars:
            if not self.has_debug and not self._flat and self._elf is not None:
                raise VarError('no_debug', f'cannot resolve {name}: the ELF has no debug information, build with -g')
            text = self._strings.get(name)
            if text is None:
                raise VarError('not_found', f'{name} not found' + _did_you_mean(_close(name, self._strings)))
            return text
        cands = self._vars.get(name)
        if not cands:
            raise VarError('not_found', f'{name} not found in the ELF' + _did_you_mean(_close(name, self._top_names())))
        if len(cands) > 1:
            raise VarError('ambiguous', f'{name} exists {len(cands)} times (static in several files)')
        _, addr, tdie = cands[0]
        typ, _, _ = self._type(tdie)
        elem = self._type(typ.elem)[0] if typ.kind == 'array' else None
        if elem is None or elem.kind != 'scalar' or elem.size != 1 or len(typ.dims) != 1 or not typ.dims[0]:
            raise VarError('not_string', f'{name} is not an array of char')
        raw = self._flash_bytes(addr, typ.dims[0])
        if raw is None:
            raise VarError('not_string', f'{name} at 0x{addr:04X} is not in the flash image of the ELF '
                                         '(a copy in RAM is initialised at start, its text is not stored there)')
        end = raw.find(b'\0')
        if end < 0:
            raise VarError('not_string', f'{name} has no end of text within its {len(raw)} bytes')
        return raw[:end].decode('latin-1')

    def _flash_bytes(self, addr, n):
        """n data bytes at data address addr from a flash section of the file, or None. See read_string."""
        if self._elf is None:
            return None
        for sec in self._elf.iter_sections():
            fl = sec['sh_flags']
            lo, size = sec['sh_addr'], sec['sh_size'] // 2          # data bytes the section holds
            # flash: allocated, not writable (the RAM sections .data and .ndata have 1 byte per byte)
            if sec['sh_type'] != 'SHT_PROGBITS' or not fl & 2 or fl & 1 or not lo <= addr or addr + n > lo + size:
                continue
            data = sec.data()
            out = bytearray()
            for a in range(addr - lo, addr - lo + n):
                out.append(data[4 * (a >> 1) + (a & 1)])
            return bytes(out)
        return None

    # -- names --------------------------------------------------------------------------------

    def names(self):
        """Sorted names that lookup() accepts; structs and arrays expanded (at most MAX_PER_VAR per variable)."""
        if self._names is None:
            if self._flat or not self._vars:
                self._names = sorted(n for n, (a, s, _) in self._flat.items() if s in (1, 2, 4) and a + s <= RAM_END)
            else:
                out = []
                for n, lst in self._vars.items():
                    for fname, addr, tdie in lst:
                        one = []
                        self._walk(one, f'{fname}:{n}' if len(lst) > 1 else n, tdie, addr, 0)
                        out += one
                self._names = sorted(out)
        return self._names

    def _walk(self, out, name, tdie, addr, depth):
        if len(out) >= MAX_PER_VAR or depth > 8:
            return
        typ, _, _ = self._type(tdie)
        if typ.kind == 'scalar':
            if typ.scalar and addr + typ.size <= RAM_END:
                out.append(name)
        elif typ.kind == 'struct':
            for mname, off, mdie, bits, _, _, _ in typ.members:
                if bits is None:
                    self._walk(out, f'{name}.{mname}', mdie, addr + off, depth + 1)
        elif typ.kind == 'array':
            self._walk_array(out, name, typ, addr, depth)

    def _walk_array(self, out, name, typ, addr, depth):
        et, _, _ = self._type(typ.elem)
        n = typ.dims[0] or 0
        inner = typ.dims[1:]
        esize = et.size * _prod(inner) if inner else et.size
        for i in range(n):
            if len(out) >= MAX_PER_VAR:
                return
            if inner:
                self._walk_array(out, f'{name}[{i}]', _Type('array', esize, elem=typ.elem, dims=inner), addr + i * esize, depth + 1)
            else:
                self._walk(out, f'{name}[{i}]', typ.elem, addr + i * esize, depth + 1)


def _prod(xs):
    p = 1
    for x in xs:
        p *= x or 0
    return p
