# Third-party notices (design system and fonts)

Added by RPT-04. This file records the licences of design and font material that ships in this repository. Python and other package dependencies are pinned in `uv.lock` and audited by the CI steps in `docs/CI_GUARDRAILS.md`; they are not repeated here.

A licence for code or fonts is **not** a licence to use any government emblem, logo or official name. This project uses none (ADR 0004; `docs/research/UI_BEST_PRACTICE.md` section 1, Dubai Law No. (1) of 2025).

## 1. UAE Design System (design tokens only)

- What we took: a small set of design values (font stacks, the type scale, the "aeblack" neutral ramp, the roles of the font families). No source files, components or class names were copied, and nothing from its Tailwind plugin is built or shipped. The exact values and where each came from are in `docs/UI_FOUNDATION.md`, "Tokens".
- Upstream: https://github.com/TDRA-ae/aegov-dls, npm package `@aegov/design-system` version 3.0.7 (published 2026-04-21). Read at commit `d309bb51cf061e3d10844598748b256c39df2f16` (2026-06-02).
- Licence: **MIT**. Verified three ways on 2026-10-13: the `LICENSE` file in that repository; `"license": "MIT"` in its `package.json` and in the npm registry record for 3.0.7.
- Copyright line in that `LICENSE`: "Copyright (c) 2023 Telecommunication and Digital Government Regulatory Authority - United Arab Emirates".

The MIT licence requires its copyright notice and permission notice to be included in "all copies or substantial portions of the Software". We reproduce the values only, not a substantial portion of the software, but we include the full notice here anyway:

```
MIT License

Copyright (c) 2023 Telecommunication and Digital Government Regulatory Authority - United Arab Emirates

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Not covered by the MIT licence and not used here: the UAE logo and gold identity shown in that repository's README, and the federal gold palette (see `docs/UI_FOUNDATION.md`, "What we did not take").

## 2. Fonts (self-hosted, `src/incident_report/web/static/fonts/`)

| Family | Files | Licence | Copyright line in the licence file | Source |
|---|---|---|---|---|
| Roboto (variable, weights 400 to 700) | `roboto-latin.woff2`, `roboto-latin-ext.woff2` | SIL Open Font License 1.1 | Copyright 2011 The Roboto Project Authors (https://github.com/googlefonts/roboto-classic) | Google Fonts, `fonts.gstatic.com/s/roboto/v51/` |
| Noto Kufi Arabic (variable, weights 400 to 700) | `noto-kufi-arabic-arabic.woff2` | SIL Open Font License 1.1 | Copyright 2022 The Noto Project Authors (https://github.com/notofonts/arabic) | Google Fonts, `fonts.gstatic.com/s/notokufiarabic/v27/` |

- The full licence texts are next to the fonts: `OFL-Roboto.txt` and `OFL-NotoKufiArabic.txt` (downloaded 2026-10-13 from `github.com/google/fonts`, `ofl/roboto/OFL.txt` and `ofl/notokufiarabic/OFL.txt`). The OFL requires the copyright notice and licence to travel with the fonts; that is why they sit in the same folder.
- Licence verified from two sources on 2026-10-13: `license: "OFL"` in the `METADATA.pb` of each family in `github.com/google/fonts`, and `license: "OFL"` for both families in the vendored `ui-ux-pro-max` data file `google-font-licenses.json`.
- Neither licence file names a Reserved Font Name (the text only defines the term; no name is declared after the copyright line), so using the files unchanged under their own names is within the licence. The files are the woff2 subsets Google serves, byte for byte as downloaded. SHA-256 of each file, so a later change is visible:

```
1404ca348bd75ef836f4dd8b6f2cc719458642d1237c368296b2fc652dca47dc  roboto-latin.woff2
cedb374b05a35034cf96db185db4eeb8f8ce49e1a56197673702ff11b5533d6e  roboto-latin-ext.woff2
01290c72f7179c551d8eef0b5e190615b082a5876e3abacca88e341e0e1fafe9  noto-kufi-arabic-arabic.woff2
```

- The OFL permits use, embedding and redistribution with software and forbids selling the font files on their own. They are shipped as part of this application only.

### Could not verify

- The Google Fonts website pages (fonts.google.com, its licensing FAQ) and designsystem.gov.ae were unreachable from the build sandbox (the proxy refused the connection). The font licence facts above therefore come from the `google/fonts` repository (raw files) and the licence files themselves, and the UAE Design System facts come from its repository and npm package, not from the websites. The "Roboto + Noto Kufi Arabic" pairing and the Google Fonts source are stated in `docs/research/UI_BEST_PRACTICE.md` section 1; the upstream stylesheet (`src/base/root.css`) confirms Roboto for body text and Noto Kufi Arabic for right-to-left body text, but also names Inter and Alexandria for headings (see `docs/UI_FOUNDATION.md`, "Deviations from upstream").
- Dubai Font (the alternative the research note mentions) is not used; its licence remains unread (`docs/research/UI_BEST_PRACTICE.md` section 1).
