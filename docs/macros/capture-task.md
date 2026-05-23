# Capture-task Templater macro (Slice 3)

The `Create Lithos task` macro prompts you for the capture form, calls `lithos-loom task create`, and inserts the resulting projected line at cursor. The line it inserts is **byte-equal** to what the projection will rewrite into `_lithos/tasks.md` on the next sync — that's US25's "born projected" guarantee: no Obsidian-only "captured but not yet promoted" intermediate state.

## What it does

1. Calls `lithos-loom project list --format json` to populate the project autocomplete dropdown.
2. Prompts for project (autocomplete), title (defaults to current selection), brief, scheduled date, priority, tags.
3. Shells out to `lithos-loom task create --project ... --title ... [...]`.
4. On success: inserts the printed projected line at cursor (or appends to `--target-file` if you wire that flag in).
5. On failure: shows the stderr message in an Obsidian Notice popup for 10 seconds.

## Prerequisites

- The `lithos-loom` binary is on `PATH` for the Obsidian process (`which lithos-loom` returns a path you can run from a fresh shell).
- The [Templater](https://github.com/SilentVoid13/Templater) community plugin is installed and enabled.
- A `[projects.<slug>]` table exists in your `lithos-loom.toml` for at least one project — the macro's project picker will surface nothing if `lithos-loom project list` returns `[]`.

## Install

This macro is a Templater **template** (it uses the `<%* ... %>` execution block and `tp.*` template helpers), so it lives in the Template Folder — not the User Script Functions Folder (that one is for plain `.js` files exported as `tp.user.<name>` functions; see the [Templater script-user-functions docs](https://silentvoid13.github.io/Templater/user-functions/script-user-functions.html)).

1. **Pick a template folder** in your vault if you don't already have one (e.g. `_meta/templates/`). Tell Templater about it: `Settings → Templater → Template Folder Location` and set it to that folder. (See the [Templater settings docs](https://silentvoid13.github.io/Templater/settings.html).)
2. **Save the template** (the file below) as `<your-template-folder>/capture-task.md`. The `.md` extension is required — Templater scans the Template Folder for `.md` files.
3. **Bind a hotkey:** `Settings → Templater → Template Hotkeys` → "Add new hotkey for template" → pick `capture-task.md`. Then `Settings → Hotkeys` → search for "Templater: capture-task" and assign your hotkey (e.g. `Mod+Shift+L`). The two-step is intentional: the Templater pane registers the command, the global Hotkeys pane binds the keystroke.
4. **Sanity check:** place the cursor in any note, fire the hotkey. The project picker should appear immediately. If nothing happens, confirm Template Folder Location is set correctly and the file appears under `Settings → Templater → Template Hotkeys`.

## Macro source

Copy this verbatim into `<your-template-folder>/capture-task.md`:

````markdown
<%*
// capture-task.md — Slice 3 capture macro.
//
// Calls `lithos-loom task create`, inserts the resulting projected
// line at cursor. The line shape matches what the projection writes
// (US25 "born projected"), so the next sync is a no-op merge.

const { execFileSync, execSync } = require("child_process");

// ── 1. Project autocomplete ────────────────────────────────────────
let projects;
try {
  const out = execSync("lithos-loom project list --format json", {
    encoding: "utf-8",
  });
  projects = JSON.parse(out);
} catch (e) {
  const msg = (e.stderr && e.stderr.toString()) || e.message;
  new Notice(`Failed to load projects from lithos-loom:\n${msg}`, 10000);
  return;
}
if (!projects || projects.length === 0) {
  new Notice(
    "No projects configured. Add a [projects.<slug>] table to your "
      + "lithos-loom.toml.",
    10000,
  );
  return;
}

const project = await tp.system.suggester(p => p, projects, true, "Project");
if (!project) return;

// ── 2. Form prompts ────────────────────────────────────────────────
const defaultTitle = tp.file.selection() || "";
const title = await tp.system.prompt("Title", defaultTitle);
if (!title) return;

const brief = await tp.system.prompt("Brief (optional)", "");
const scheduled = await tp.system.prompt(
  "Scheduled date (YYYY-MM-DD, optional)",
  "",
);
const priority = await tp.system.suggester(
  p => p || "(none)",
  ["", "highest", "high", "medium", "low", "lowest"],
  false,
  "Priority (optional)",
);
const tags = await tp.system.prompt(
  "Tags (comma-separated, optional)",
  "",
);

// ── 3. Build argv and shell out ────────────────────────────────────
const args = ["task", "create", "--project", project, "--title", title];
if (brief)     args.push("--brief", brief);
if (scheduled) args.push("--scheduled", scheduled);
if (priority)  args.push("--priority", priority);
if (tags)      args.push("--tags", tags);

let line;
try {
  line = execFileSync("lithos-loom", args, { encoding: "utf-8" }).trimEnd();
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString().trim()) || e.message;
  new Notice(`lithos-loom task create failed:\n${stderr}`, 10000);
  return;
}

// ── 4. Insert the projected line at cursor ─────────────────────────
tR += line + "\n";
%>
````

## Behavior notes

- **"Born projected"**: the line `lithos-loom task create` prints is generated by the shared `lithos_loom.render` module that the projection subscription also uses. So a macro-inserted line, the projection's next rewrite of `_lithos/tasks.md`, and the fs-watcher's self-write suppression all agree on the same content. The macro can drop the line into your daily note, an inbox file, or `_lithos/tasks.md` directly and no duplication will result.
- **Selection-as-title**: if you've highlighted text before invoking the macro, that text is the default title. Lets you turn a phrase into a task with one hotkey.
- **Skipping fields**: empty inputs at any prompt are forwarded as omitted CLI flags. `lithos-loom task create` then leaves the corresponding metadata key absent.
- **Tags**: passed as comma-separated; the CLI strips whitespace and drops empty entries. So `"foo, , bar"` becomes `["foo", "bar"]`.
- **Errors**: any non-zero exit from `lithos-loom` (unknown project, network failure, Lithos validation envelope) surfaces in a Notice popup with the stderr message. The macro returns without inserting anything.
- **Lithos availability**: the macro doesn't pre-check Lithos health. If the daemon is down, the `task create` invocation surfaces the connection error directly. Run `lithos-loom doctor` first if the macro silently no-ops.

## Optional CLI flags (US27)

The CLI ships two output-mode flags so the same `lithos-loom task create` invocation powers more than just the cursor-insert flow this macro defaults to. The two flags are mutually exclusive — passing both is a usage error (exit 2).

### `--target-file PATH`

Appends the projected line to `PATH` instead of printing to stdout (nothing is printed). Useful for "create a task and put it in next week's daily note" flows. The file is created (with parent dirs) if it doesn't exist. To wire it into this macro, add an optional prompt before the argv build:

```javascript
const targetFile = await tp.system.prompt(
  "Target file (optional, leave empty for cursor insert)",
  "",
);
// ... later:
if (targetFile) args.push("--target-file", targetFile);
```

When `--target-file` is given, the CLI writes the line directly and prints nothing to stdout — adjust the macro's insert step accordingly (e.g. wrap the `tR += line + "\n"` in `if (!targetFile)`).

### `--no-insert`

Creates the task but discards the projected line — stdout gets just the new task's id. Useful for shell-script callers that only need the id back, e.g. inside another script that chains "create task → assign it to a process". The macro doesn't typically need this (the macro's whole point is to insert), but it's the documented escape hatch for "I want the side effect without the line":

```sh
task_id=$(lithos-loom task create --project lithos-loom --title "Track this" --no-insert)
echo "created $task_id"
```

Combining `--no-insert` with `--target-file` is a usage error — the two flags answer the same question ("where does the line go?") differently.
