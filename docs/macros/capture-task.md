<%*
// capture-task.md — Slice 3 capture macro for lithos-loom.
//
// Install: copy this file (verbatim) into your vault's Templater
// Template Folder, then bind Obsidian's "Templater: Insert
// capture-task" command to a hotkey. Full instructions and
// behaviour notes live in docs/macros/README.md.
//
// What it does:
//   1. Loads project list via `lithos-loom project list --format json`
//   2. Opens a single Obsidian Modal with all six fields visible
//   3. Calls `lithos-loom task create --no-insert ...`
//   4. Inserts a wiki-link at cursor pointing at _lithos/tasks.md
//      (the daemon's projection writes the canonical task line into
//      that file independently — we never duplicate the line here)

const { execSync, execFileSync } = require("child_process");
const obsidian = require("obsidian");

// 1. Load project list AND obsidian_sync config from Loom. The
//    tasks_file path is operator-configurable; hardcoding the
//    default would break the wikilink on hosts that customise it.
let projects;
let tasksFile;
try {
  projects = JSON.parse(
    execSync("lithos-loom project list --format json", { encoding: "utf-8" })
  );
  const obsCfg = JSON.parse(
    execSync("lithos-loom obsidian-sync show --format json", {
      encoding: "utf-8",
    })
  );
  tasksFile = obsCfg.tasks_file;
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString()) || e.message;
  new obsidian.Notice(`Failed to load Loom config:\n${stderr}`, 10000);
  return;
}
if (!projects.length) {
  new obsidian.Notice(
    "No projects configured. Add a [projects.<slug>] table to your lithos-loom.toml.",
    10000
  );
  return;
}

// 2. Custom Modal with all fields in one dialog.
class CaptureModal extends obsidian.Modal {
  constructor(app, projects, defaultTitle, onSubmit) {
    super(app);
    this.projects = projects;
    this.result = {
      project: projects[0],
      title: defaultTitle || "",
      brief: "",
      scheduled: "",
      priority: "",
      tags: "",
    };
    this.submitted = false;
    this.onSubmit = onSubmit;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.createEl("h2", { text: "Capture Lithos task" });

    new obsidian.Setting(contentEl).setName("Project").addDropdown((dd) => {
      this.projects.forEach((p) => dd.addOption(p, p));
      dd.setValue(this.result.project).onChange((v) => (this.result.project = v));
    });

    new obsidian.Setting(contentEl).setName("Title").addText((t) => {
      t.setValue(this.result.title).onChange((v) => (this.result.title = v));
      // Auto-focus title after render so Tab order starts here.
      setTimeout(() => t.inputEl.focus(), 0);
      // Enter-to-submit when the title field has focus.
      t.inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this.submit();
        }
      });
    });

    new obsidian.Setting(contentEl).setName("Brief (optional)").addTextArea((ta) => {
      ta.onChange((v) => (this.result.brief = v));
    });

    new obsidian.Setting(contentEl)
      .setName("Scheduled (YYYY-MM-DD, optional)")
      .addText((t) => {
        t.onChange((v) => (this.result.scheduled = v));
      });

    new obsidian.Setting(contentEl).setName("Priority").addDropdown((dd) => {
      ["", "highest", "high", "medium", "low", "lowest"].forEach((p) =>
        dd.addOption(p, p || "(none)")
      );
      dd.onChange((v) => (this.result.priority = v));
    });

    new obsidian.Setting(contentEl)
      .setName("Tags (comma-separated, optional)")
      .addText((t) => {
        t.onChange((v) => (this.result.tags = v));
      });

    new obsidian.Setting(contentEl)
      .addButton((b) =>
        b
          .setButtonText("Create")
          .setCta()
          .onClick(() => this.submit())
      )
      .addButton((b) => b.setButtonText("Cancel").onClick(() => this.close()));
  }

  submit() {
    if (!this.result.title.trim()) {
      new obsidian.Notice("Title is required", 3000);
      return;
    }
    this.submitted = true;
    this.close();
  }

  onClose() {
    this.contentEl.empty();
    this.onSubmit(this.submitted ? this.result : null);
  }
}

// 3. Open modal, await result.
const form = await new Promise((resolve) => {
  new CaptureModal(app, projects, tp.file.selection() || "", resolve).open();
});
if (!form) return;

// 4. Build argv. --no-insert returns just the task_id; the daemon's
//    projection subscription writes the canonical line into
//    _lithos/tasks.md independently when Lithos broadcasts
//    task.created. We never insert the task line here — that would
//    create a stale duplicate.
const args = [
  "task", "create", "--no-insert",
  "--project", form.project,
  "--title", form.title,
];
if (form.brief)     args.push("--brief", form.brief);
if (form.scheduled) args.push("--scheduled", form.scheduled);
if (form.priority)  args.push("--priority", form.priority);
if (form.tags)      args.push("--tags", form.tags);

// 5. Shell out; capture task_id from stdout.
let taskId;
try {
  taskId = execFileSync("lithos-loom", args, { encoding: "utf-8" }).trim();
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString().trim()) || e.message;
  new obsidian.Notice(`lithos-loom task create failed:\n${stderr}`, 10000);
  return;
}

// 6. Sanitise title for wikilink display text — Obsidian wikilink
//    syntax breaks on [ ] | and newlines.
const safeTitle = form.title.replace(/[\[\]\|]/g, " ").replace(/\s+/g, " ").trim();

// 7. Insert wiki-link at cursor. Title is the clickable bit; the
//    trailing 🆔 lithos:<id> is greppable from anywhere in the vault.
//    `tasksFile` came from `lithos-loom obsidian-sync show` so it
//    respects any operator-customised `obsidian_sync.tasks_file`.
tR += `[[${tasksFile}|${safeTitle}]] 🆔 lithos:${taskId}\n`;
%>
