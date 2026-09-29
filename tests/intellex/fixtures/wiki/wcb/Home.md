# WCB HIL Home

A crafted page for the Intellex HIL tests (tests/intellex). Nothing here is real documentation. Every link, image and
script below is something the offline docs viewer must rewrite or keep inert.

## Links

- [[Getting Started]]
- [[Shown text|Target Page]]
- [A dash link](Page-Name)
- [A page with a file suffix](Other-Page.md)
- [External](https://example.com/)
- [Anchor only](#links)

## Images

![present image](Images/pic.png)

![missing image](Images/not-downloaded.png)

<img src="Images/pic.png" alt="raw present">

<img src="Images/gone.png" alt="raw gone">

## Script that must not run

<script>window.__pwned = 'inline script';</script>

<img src="data:," alt="onerror sentinel" onerror="window.__pwned = 'onerror'">

[markdown js link](javascript:window.__pwned='md-js')

<a id="rawjs" href="javascript:window.__pwned='raw-js'">raw js link</a>

## Code stays code

```text
[[Not A Link]] https://example.com/in-code <img src="Images/pic.png">
```

Inline code too: `[[Inline Not A Link]]`.
