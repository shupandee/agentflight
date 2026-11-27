# Vendored files

| File | Project | Version | License | Source |
| --- | --- | --- | --- | --- |
| `htmx-2.0.11.min.js` | [htmx](https://htmx.org) | 2.0.11 | 0BSD (Zero-Clause BSD) | `dist/htmx.min.js` from the npm package `htmx.org@2.0.11` |

The npm tarball matched the registry integrity
`sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==`.
SHA-256 of the vendored file:
`d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`.

It's served from `/ui/static`, so the viewer's Content-Security-Policy can stay
`script-src 'self'` with no CDN. To upgrade, replace the file, update the version in its
name, in `templates/base.html` and here, and re-check the hashes.

htmx license text:

```
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```
