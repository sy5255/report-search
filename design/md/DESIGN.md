# Design System Document: The Intellectual Curator

## 1. Overview & Creative North Star
**Creative North Star: The Digital Archive**
This design system rejects the frantic, "noisy" interface of standard AI tools in favor of a "Digital Archive" aesthetic. It is a philosophy of quiet authority, inspired by high-end editorial journals and archival research papers. We move beyond the "template" look by utilizing intentional asymmetry, expansive whitespace, and a high-contrast typographic pairing of humanist serifs and technical geometric sans-serifs.

The goal is to create a "Sanctuary of Information." Instead of overwhelming the user with cards and borders, we use tonal depth and layered surfaces to guide the eye through complex markdown data. It should feel less like a software application and more like a curated scholarly exhibition.

---

## 2. Colors & Surface Philosophy

The palette is rooted in the depth of `primary` (#1e293b) and the clarity of `surface_container_lowest` (#ffffff).

### The "No-Line" Rule
Standard 1px borders are strictly prohibited for sectioning. We define space through **Tonal Transitions**. To separate a sidebar from a main chat area, transition from `surface_container` (#eceef0) to `surface` (#f7f9fb). Content blocks are defined by their background shift, not an outline.

### Surface Hierarchy & Nesting
Treat the interface as a physical stack of materials:
* **Base Layer:** `background` (#f7f9fb) — The desk surface.
* **Sidebar/Navigation:** `surface_container_low` (#f2f4f6) — A recessed area.
* **Main Chat Canvas:** `surface_container_lowest` (#ffffff) — The primary sheet of paper.
* **AI Response Blocks:** `surface_container_high` (#e6e8ea) — A subtle lift to denote "machine-generated" content.

### The "Glass & Gradient" Rule
For floating elements like "Scroll to bottom" buttons or "Source" popovers, use **Glassmorphism**:
* **Color:** `surface_variant` at 70% opacity.
* **Blur:** 12px-20px backdrop-blur.
* **Texture:** Primary CTAs should utilize a subtle linear gradient from `primary` (#1e293b) to `primary_container` (#343f52) at a 135-degree angle to provide a satin-like finish.

---

## 3. Typography: The Editorial Voice

We employ a dual-font strategy to balance character with utility.

* **Headlines (Manrope):** Used for "Display" and "Headline" scales. This font brings a modern, geometric authority to the interface. Use `headline-lg` for session titles to establish an immediate sense of importance.
* **Content (Inter):** Used for "Title," "Body," and "Label." Inter is selected for its high legibility in dense RAG data environments.
* **The Trust Factor:** Citations and "Source" references must use `label-md` or `label-sm` in `secondary` (#505f76) color. This "fine print" should feel like a legal or academic footnote—small, precise, and indisputable.

---

## 4. Elevation & Depth: Tonal Layering

Traditional drop shadows are replaced by **Ambient Occlusion** and **Tonal Stacking**.

* **The Layering Principle:** Instead of a shadow, place a `surface_container_lowest` card inside a `surface_container` area. The 4-5% shift in lightness creates a sophisticated, "soft" depth.
* **Ambient Shadows:** If a modal or floating menu is required, use a `0px 20px 40px` blur. The shadow color must be a 6% opacity version of `on_surface` (#191c1e).
* **The Ghost Border:** For accessibility in input fields, use `outline_variant` (#c4c6cd) at 20% opacity. It should be felt, not seen.

---

## 5. Components & Interface Elements

### Message Architecture
* **User Messages:** Right-aligned, utilizing `primary` background with `on_primary` text. Use `xl` (0.75rem) roundedness on all corners except the bottom right.
* **AI Responses:** Left-aligned, no background container—text sits directly on the `surface_container_lowest` canvas. Use high-contrast Markdown styling:
* **Bold text:** Use `tertiary` (#21283c) to make it "pop" from the body text.
* **Code Blocks:** Background `inverse_surface` (#2d3133) with `md` (0.375rem) corner radius.
* **Source Citations:** Displayed as "Pills" using `secondary_container` (#d0e1fb) with `label-sm` typography. These should appear immediately following a claim, mimicking academic citations.

### Inputs & Buttons
* **The Chat Bar:** Use `surface_container_highest` (#e0e3e5) for the text area background. No border. Apply `lg` (0.5rem) roundedness.
* **Primary Button:** Gradient fill (Primary to Primary Container), `full` roundedness, `body-md` bold text.
* **Secondary/Action Chips:** Use `surface_variant` backgrounds. Forbid the use of dividers between chips; use 8px of horizontal spacing instead.

### RAG-Specific Components
* **Reference Drawer:** A side-aligned panel using `surface_container_low`. Content should be separated by vertical whitespace (24px-32px) rather than horizontal rules.
* **Confidence Meter:** A subtle 2px-high progress bar at the top of an AI response, using `surface_tint` (#545f73) to indicate the RAG retrieval strength.

---

## 6. Do’s and Don’ts

### Do:
* **Embrace Whitespace:** If a section feels crowded, double the padding. This system relies on "breathing room" to feel premium.
* **Use Asymmetric Margins:** Align text-heavy responses with a wider left margin (e.g., 64px) to create an editorial "gutter."
* **Style Markdown:** Headers within AI responses should use `title-sm` in `primary` color to ensure a clear information hierarchy.

### Don’t:
* **Don't Use Pure Black:** Always use `on_background` (#191c1e) for text to maintain a soft, ink-on-paper feel.
* **Don't Use 1px Dividers:** To separate chat history dates or message groups, use a 12px `surface_variant` height gap or a subtle background color shift.
* **Don't Over-Round:** Stick to the defined `Roundedness Scale`. Avoid "bubbly" UI by keeping cards at `lg` (0.5rem) and only using `full` for functional chips and buttons.