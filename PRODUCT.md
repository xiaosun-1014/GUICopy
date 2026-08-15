# Product

## Register

product

## Users

Developers and clinical-imaging automation engineers who record DICOM web-viewer workflows, generate offline replicas, and inspect deterministic validation artifacts on local Windows workstations.

## Product Purpose

Turn recorded Playwright workflows and semantic markers into executable adapters and faithful offline replicas. Success means the replica preserves the captured application's visible state, usable DOM structure, interactions, and metadata outputs while remaining deterministic and safe to inspect without the source system.

## Brand Personality

Precise, dependable, and unobtrusive. The interface should feel like an engineering instrument that preserves the source application's behavior, not a redesigned interpretation of it.

## Anti-references

Avoid generic dashboard styling, decorative reinterpretation of captured clinical interfaces, screenshot layers that conceal or duplicate functional DOM, fixed-size canvases that break outside the capture viewport, and interactions that only appear correct at one resolution.

## Design Principles

1. Preserve source fidelity: captured structure, states, and interaction semantics take priority over visual invention.
2. Keep evidence inspectable: meaningful content should exist in usable DOM when it was captured as DOM.
3. Prefer deterministic behavior: identical captures should produce identical replicas and validation results.
4. Adapt without distortion: scale or reflow the replica predictably while keeping overlays aligned with their source coordinates.
5. Protect clinical data: keep patient images and metadata local and avoid leaking them into logs or reports.

## Accessibility & Inclusion

Use semantic DOM and keyboard-compatible controls where the source provides them. Maintain readable contrast and visible focus states for generated controls. Support common desktop viewport sizes without hidden controls, clipped dialogs, or page-level overflow that prevents access to content.
