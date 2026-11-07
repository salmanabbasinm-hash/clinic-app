import struct, sys, datetime
PS = 4096

class DB:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        self.ver = self.d[0x14]
        self.cat = None

    def page(self, n):
        return self.d[n*PS:(n+1)*PS]

    def tdef(self, pg):
        buf = bytearray()
        p = pg
        first = True
        while p:
            pb = self.page(p)
            assert pb[0] == 2, (p, pb[0])
            nxt = struct.unpack_from('<I', pb, 4)[0]
            buf += pb if first else pb[8:]
            first = False
            p = nxt
        return bytes(buf)

    def read_table(self, tdpage):
        t = self.tdef(tdpage)
        num_rows = struct.unpack_from('<I', t, 16)[0]
        num_var = struct.unpack_from('<H', t, 43)[0]
        num_cols = struct.unpack_from('<H', t, 45)[0]
        num_idx = struct.unpack_from('<I', t, 47)[0]
        num_ridx = struct.unpack_from('<I', t, 51)[0]
        up = struct.unpack_from('<I', t, 55)[0]  # row(1)+page(3)
        pos = 63 + num_ridx * 12
        cols = []
        for i in range(num_cols):
            e = t[pos:pos+25]
            typ = e[0]
            cnum = struct.unpack_from('<H', e, 5)[0]
            voff = struct.unpack_from('<H', e, 7)[0]
            flags = e[15]
            foff = struct.unpack_from('<H', e, 21)[0]
            size = struct.unpack_from('<H', e, 23)[0]
            cols.append(dict(type=typ, num=cnum, voff=voff, fixed=bool(flags & 1), foff=foff, size=size))
            pos += 25
        for c in cols:
            ln = struct.unpack_from('<H', t, pos)[0]
            c['name'] = t[pos+2:pos+2+ln].decode('utf-16le')
            pos += 2 + ln
        cols.sort(key=lambda c: c['num'])
        # owned pages usage map
        row = up & 0xff
        pg = up >> 8
        pages = self.usage_map(pg, row)
        rows = []
        for p in pages:
            pb = self.page(p)
            if pb[0] != 1:
                continue
            if struct.unpack_from('<I', pb, 4)[0] != tdpage:
                continue
            for r in self.page_rows(p):
                rows.append(self.parse_row(r, cols))
        return cols, rows

    def usage_map(self, pg, row):
        pb = self.page(pg)
        n = struct.unpack_from('<H', pb, 12)[0]
        offs = [struct.unpack_from('<H', pb, 14+2*i)[0] for i in range(n)]
        start = offs[row] & 0x1fff
        end = PS if row == 0 else (offs[row-1] & 0x1fff)
        data = pb[start:end]
        pages = []
        if data[0] == 0:
            base = struct.unpack_from('<I', data, 1)[0]
            bits = data[5:]
            for i, b in enumerate(bits):
                for k in range(8):
                    if b >> k & 1:
                        pages.append(base + i*8 + k)
        else:
            # reference map
            maps = struct.unpack_from('<' + 'I'*((len(data)-1)//4), data, 1)
            per = (PS-4)*8
            for mi, mp in enumerate(maps):
                if mp == 0: continue
                pb2 = self.page(mp)
                for i, b in enumerate(pb2[4:]):
                    for k in range(8):
                        if b >> k & 1:
                            pages.append(mi*per + i*8 + k)
        return pages

    def page_rows(self, p, include_deleted=False):
        pb = self.page(p)
        n = struct.unpack_from('<H', pb, 12)[0]
        out = []
        prev = PS
        for i in range(n):
            o = struct.unpack_from('<H', pb, 14+2*i)[0]
            deleted = o & 0x8000
            lookup = o & 0x4000
            s = o & 0x1fff
            e = prev
            prev = s
            if (deleted and not include_deleted):
                continue
            if lookup:
                # overflow pointer
                ptr = pb[s:s+4]
                rno = ptr[0]; pgn = ptr[1] | ptr[2] << 8 | ptr[3] << 16
                out.append(self.row_bytes(pgn, rno))
                continue
            out.append(pb[s:e])
        return out

    def row_bytes(self, p, r):
        pb = self.page(p)
        o = struct.unpack_from('<H', pb, 14+2*r)[0]
        s = o & 0x1fff
        e = PS if r == 0 else (struct.unpack_from('<H', pb, 14+2*(r-1))[0] & 0x1fff)
        if o & 0x4000:
            ptr = pb[s:s+4]
            return self.row_bytes(ptr[1] | ptr[2] << 8 | ptr[3] << 16, ptr[0])
        return pb[s:e]

    def parse_row(self, r, cols):
        ncols = struct.unpack_from('<H', r, 0)[0]
        bm = (ncols + 7)//8
        end = len(r) - 1
        nvar = struct.unpack_from('<H', r, end - bm - 1)[0]
        voffs = [struct.unpack_from('<H', r, end - bm - 3 - 2*i)[0] for i in range(nvar+1)]
        mask = r[len(r)-bm:]
        res = {}
        for c in cols:
            n = c['num']
            if n >= ncols:
                res[c['name']] = None; continue
            bit = (mask[n//8] >> (n % 8)) & 1
            if c['type'] == 1:
                res[c['name']] = bool(bit); continue
            if not bit:
                res[c['name']] = None; continue
            if c['fixed']:
                raw = r[2+c['foff']: 2+c['foff']+c['size']]
            else:
                vi = c['voff']
                if vi >= nvar:
                    res[c['name']] = None; continue
                raw = r[voffs[vi]: voffs[vi+1]] if voffs[vi+1] > voffs[vi] else b''
            res[c['name']] = self.conv(c, raw)
        return res

    def conv(self, c, raw):
        t = c['type']
        try:
            if t == 2: return raw[0]
            if t == 3: return struct.unpack('<h', raw)[0]
            if t == 4: return struct.unpack('<i', raw)[0]
            if t == 5: return struct.unpack('<q', raw)[0]/10000
            if t == 6: return struct.unpack('<f', raw)[0]
            if t == 7: return struct.unpack('<d', raw)[0]
            if t == 8:
                v = struct.unpack('<d', raw)[0]
                return (datetime.datetime(1899, 12, 30) + datetime.timedelta(days=v)).isoformat(sep=' ')
            if t == 10: return self.text(raw)
            if t == 9: return raw
            if t == 15: return raw.hex()
            if t in (11, 12):
                return self.memo(raw, t)
        except Exception as ex:
            return ('ERR', str(ex), raw[:20].hex())
        return raw

    def text(self, raw):
        if raw[:2] == b'\xff\xfe':
            out = []; comp = True; i = 2
            buf = bytearray()
            # compressed unicode: mode toggles on 0x00
            res = ''
            while i < len(raw):
                if comp:
                    if raw[i] == 0:
                        comp = False; i += 1; continue
                    res += chr(raw[i]); i += 1
                else:
                    if raw[i] == 0 and False:
                        pass
                    if i+1 >= len(raw): break
                    # uncompressed segment: until we hit 0x0000? mdbtools: 0 byte toggles in compressed only
                    # In uncompressed mode, read pairs; segment ends at end or when pair is 00 00? treat rest as uncompressed
                    res += raw[i:].decode('utf-16le', 'replace'); break
            return res
        return raw.decode('utf-16le', 'replace')

    def memo(self, raw, t):
        ln = struct.unpack_from('<I', raw, 0)[0]
        flag = ln >> 24
        ln &= 0xffffff
        if flag & 0x80:
            data = raw[12:12+ln]
        elif flag & 0x40:
            pg = struct.unpack_from('<I', raw, 4)[0]
            data = self.row_bytes(pg >> 8, pg & 0xff)[:ln]
        else:
            pg = struct.unpack_from('<I', raw, 4)[0]
            data = b''
            while pg:
                rb = self.row_bytes(pg >> 8, pg & 0xff)
                nxt = struct.unpack_from('<I', rb, 0)[0]
                data += rb[4:]
                pg = nxt
            data = data[:ln]
        if t == 12:
            return self.text(data)
        return data

    def catalog(self):
        cols, rows = self.read_table(2)
        return rows


if __name__ == '__main__':
    db = DB(sys.argv[1])
    for r in db.catalog():
        print(r.get('Id'), r.get('Type'), r.get('Name'), r.get('Flags'), r.get('ParentId'))
