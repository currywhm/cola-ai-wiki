export class EventStream {
  private pending: number[] = []
  private text = ''
  constructor(private receive: (data: any) => void) {}
  push(chunk: ArrayBuffer | string) {
    if (typeof chunk === 'string') { this.text += chunk; this.drain(); return }
    const bytes = this.pending.concat(Array.from(new Uint8Array(chunk))); this.pending = []
    let i = 0
    while (i < bytes.length) {
      const first = bytes[i]; const size = first < 128 ? 1 : first >= 194 && first <= 223 ? 2 : first >= 224 && first <= 239 ? 3 : first >= 240 && first <= 244 ? 4 : 0
      if (!size) { this.text += '\uFFFD'; i++; continue }
      if (i + size > bytes.length) { this.pending = bytes.slice(i); break }
      let code = first & (size === 1 ? 127 : (1 << (7 - size)) - 1); let valid = true
      for (let j = 1; j < size; j++) { if ((bytes[i + j] & 192) !== 128) { valid = false; break }; code = (code << 6) | (bytes[i + j] & 63) }
      if (!valid || (size === 2 && code < 128) || (size === 3 && code < 2048) || (size === 4 && code < 65536) || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) { this.text += '\uFFFD'; i++; continue }
      this.text += String.fromCodePoint(code); i += size
    }
    this.drain()
  }
  private drain() {
    let boundary: RegExpExecArray | null
    while ((boundary = /\r?\n\r?\n/.exec(this.text))) {
      const event = this.text.slice(0, boundary.index); this.text = this.text.slice(boundary.index + boundary[0].length)
      const data = event.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (data) this.receive(JSON.parse(data))
    }
  }
}
