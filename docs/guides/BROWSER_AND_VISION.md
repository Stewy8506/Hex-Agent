# Headless Browser & Multimodal Vision (Tier B)

HEX includes built-in headless browser automation and multimodal image inspection capabilities, allowing the agent to test web applications, inspect UI layouts, and diagnose visual regressions directly from your terminal.

---

## 1. Headless Browser Automation (`browser`)

The `browser` tool provides automated web interaction without requiring external browser installations or bulky WebDriver services.

### Core Capabilities:
- **Navigation & Page Rendering**: Navigate to remote URLs or local development servers (`http://localhost:3000`).
- **Compact Accessibility Tree (`ariaSnapshot`)**:
  - Rather than dumping raw HTML or large DOM trees, HEX extracts a concise, accessibility-oriented semantic tree.
  - Interactive elements are assigned deterministic reference IDs (e.g., `@e1`, `@e2`, `@e3`).
  - Reduces DOM token usage by over **85%**.
- **Interactive UI Actions**:
  - `click`: Click buttons, links, or inputs using element IDs or CSS selectors.
  - `type`: Enter text into form fields.
  - `select`: Choose dropdown options.
  - `evaluate`: Run arbitrary client-side JavaScript expressions.
  - `screenshot`: Capture PNG screenshots of full pages or specific elements.

### Example Tool Invocation:
```python
browser(action="navigate", url="http://localhost:3000/login")
browser(action="snapshot")
browser(action="type", selector="@e1", text="admin@example.com")
browser(action="click", selector="@e3")
browser(action="screenshot", output_path="login_result.png")
```

---

## 2. Multimodal Visual Inspection (`image_inspect`)

The `image_inspect` tool allows HEX to evaluate local and remote images using multimodal frontier vision models (e.g. Gemini 2.5 Pro / Flash, Claude 3.7 Sonnet, GPT-4o):

### Features:
- **Format Support**: PNG, JPG, JPEG, WEBP, GIF, SVG.
- **Visual Regression Testing**: Compare rendered UI components against design specifications.
- **Layout Alignment Verification**: Identify misaligned margins, overflow issues, or broken font renderings.
- **Automatic Dimension & MIME Resolution**: Automatically converts images to optimized base64 payloads for model reasoning.

### Example Tool Invocation:
```python
image_inspect(
    image_path="login_result.png",
    prompt="Verify if the submit button is properly aligned with the password input field."
)
```
