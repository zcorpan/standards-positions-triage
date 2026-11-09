## Request for Mozilla Position on an Emerging Web Specification

* Specification Title: URL
* Specification or proposal URL (if available): https://url.spec.whatwg.org/
* Explainer URL (if available): N/A
* Caniuse.com URL (optional): N/A
* Bugzilla URL (optional): N/A
* Mozillians who can provide input (optional): @martinthomson @zcorpan 

### Other information

This is part of Interop 2023 and will result in multiple changes in Chrome. So far we have:

- [Intent to Ship: URL Standard-compatible IPv4 embedded IPv6 host parser](https://groups.google.com/a/chromium.org/g/blink-dev/c/4Fkgx4rh574/m/iM8b4SRQAwAJ)
- [Intent to Ship: Port overflow check in URL setters](https://groups.google.com/a/chromium.org/g/blink-dev/c/xsITedSTDnM/m/ANFrwCN0BgAJ)
- [Intent to Ship: Support URLs with non-special schemes](https://groups.google.com/a/chromium.org/g/blink-dev/c/wYuPrIQzDTA/m/uoL4bXR2BgAJ)
- [Intent to Ship: Forbidden and escaped host characters compliant with the URL standard](https://groups.google.com/a/chromium.org/g/blink-dev/c/ZkeDSeuBx0Q/m/DO750FkoBAAJ)

@zcorpan suggested that we file positions for changes even if they're part of Interop 2023, so this issue is intended to cover everything in it. In other words, anything that's currently failing in Chrome here:
https://wpt.fyi/results/url?label=experimental&label=master&product=chrome&product=firefox&product=safari&aligned&view=interop&q=label%3Ainterop-2023-url