use crate::db::{Db, DuplicatePair, Memory};
use crate::duplicate::find_duplicates_async;
use crate::editor;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Mode {
    Overview,
    Memories,
    Review,
    Operations,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum InputMode {
    Normal,
    Search,
    ProjectFilter,
    Command,
}

pub struct App {
    pub db: Db,
    pub db_path: PathBuf,
    pub mode: Mode,
    pub input_mode: InputMode,
    pub project_filter: String,
    pub include_archived: bool,
    pub should_quit: bool,
    pub show_help: bool,
    pub show_confirm: bool,
    pub confirm_message: String,
    pub confirm_action: Option<ConfirmAction>,

    // Search/command input
    pub input_buffer: String,
    pub search_query: String,
    pub category_filter: String,

    // Overview
    pub stats_total: i64,
    pub stats_active: i64,
    pub stats_archived: i64,
    pub stats_projects: Vec<(String, i64)>,
    pub stats_categories: Vec<(String, i64)>,
    pub stats_recent: Vec<Memory>,

    // Memories
    pub memories: Vec<Memory>,
    pub memory_selected: usize,
    pub memory_detail: Option<Memory>,

    // Review
    pub duplicates: Vec<DuplicatePair>,
    pub duplicate_selected: usize,
    pub duplicate_loading: bool,
    pub duplicate_result: Option<Arc<Mutex<Option<Vec<DuplicatePair>>>>>,
    pub ignored_pairs: HashSet<(String, String)>,

    // Operations log
    pub op_log: Vec<String>,

    // Notification
    pub notification: Option<String>,
    pub notification_timer: u8,
}

#[derive(Debug, Clone)]
pub enum ConfirmAction {
    ArchiveMemory(String),
    RestoreMemory(String),
    MergePair(String, String),
    ArchiveDuplicate(String),
}

impl App {
    pub fn new(db: Db, db_path: PathBuf, project: String, include_archived: bool) -> Self {
        App {
            db,
            db_path,
            mode: Mode::Overview,
            input_mode: InputMode::Normal,
            project_filter: project,
            include_archived,
            should_quit: false,
            show_help: false,
            show_confirm: false,
            confirm_message: String::new(),
            confirm_action: None,
            input_buffer: String::new(),
            search_query: String::new(),
            category_filter: String::new(),
            stats_total: 0,
            stats_active: 0,
            stats_archived: 0,
            stats_projects: Vec::new(),
            stats_categories: Vec::new(),
            stats_recent: Vec::new(),
            memories: Vec::new(),
            memory_selected: 0,
            memory_detail: None,
            duplicates: Vec::new(),
            duplicate_selected: 0,
            duplicate_loading: false,
            duplicate_result: None,
            ignored_pairs: HashSet::new(),
            op_log: Vec::new(),
            notification: None,
            notification_timer: 0,
        }
    }

    pub fn notify(&mut self, msg: &str) {
        self.notification = Some(msg.to_string());
        self.notification_timer = 30; // ~3 seconds at 10fps
    }

    pub fn tick(&mut self) {
        if self.notification_timer > 0 {
            self.notification_timer -= 1;
            if self.notification_timer == 0 {
                self.notification = None;
            }
        }

        // Check if duplicate search finished
        if self.duplicate_loading {
            let mut finished = None;
            if let Some(ref result) = self.duplicate_result {
                if let Ok(guard) = result.try_lock() {
                    if guard.is_some() {
                        finished = guard.clone();
                    }
                }
            }
            if let Some(pairs) = finished {
                let ignored = &self.ignored_pairs;
                let filtered: Vec<DuplicatePair> = pairs
                    .into_iter()
                    .filter(|p| !ignored.contains(&(p.left_id.clone(), p.right_id.clone())))
                    .collect();
                self.duplicates = filtered;
                self.duplicate_selected = 0;
                self.duplicate_loading = false;
                self.duplicate_result = None;
            }
        }
    }

    pub fn refresh_overview(&mut self) {
        let project = if self.project_filter.is_empty() {
            None
        } else {
            Some(self.project_filter.as_str())
        };
        if let Ok(stats) = self.db.get_stats(project) {
            self.stats_total = stats.total;
            self.stats_active = stats.active;
            self.stats_archived = stats.archived;
            self.stats_projects = stats.projects;
            self.stats_categories = stats.categories;
            self.stats_recent = stats.recent;
        }
    }

    pub fn refresh_memories(&mut self) {
        let project = if self.project_filter.is_empty() {
            None
        } else {
            Some(self.project_filter.as_str())
        };
        let category = if self.category_filter.is_empty() {
            None
        } else {
            Some(self.category_filter.as_str())
        };
        let query = if self.search_query.is_empty() {
            None
        } else {
            Some(self.search_query.as_str())
        };

        if let Ok(mems) =
            self.db
                .list_memories(300, project, category, self.include_archived, query)
        {
            self.memories = mems;
            self.memory_selected = 0;
            self.update_detail();
        }
    }

    pub fn refresh_duplicates(&mut self) {
        self.duplicate_loading = true;
        let project = if self.project_filter.is_empty() {
            None
        } else {
            Some(self.project_filter.clone())
        };
        self.duplicate_result = Some(find_duplicates_async(self.db_path.clone(), project, 100));
    }

    pub fn update_detail(&mut self) {
        if let Some(mem) = self.memories.get(self.memory_selected) {
            self.memory_detail = self.db.get_memory(&mem.id).ok().flatten();
        } else {
            self.memory_detail = None;
        }
    }

    pub fn move_down(&mut self) {
        match self.mode {
            Mode::Memories => {
                if self.memory_selected < self.memories.len().saturating_sub(1) {
                    self.memory_selected += 1;
                    self.update_detail();
                }
            }
            Mode::Review => {
                if self.duplicate_selected < self.duplicates.len().saturating_sub(1) {
                    self.duplicate_selected += 1;
                }
            }
            _ => {}
        }
    }

    pub fn move_up(&mut self) {
        match self.mode {
            Mode::Memories => {
                if self.memory_selected > 0 {
                    self.memory_selected -= 1;
                    self.update_detail();
                }
            }
            Mode::Review => {
                if self.duplicate_selected > 0 {
                    self.duplicate_selected -= 1;
                }
            }
            _ => {}
        }
    }

    pub fn move_top(&mut self) {
        match self.mode {
            Mode::Memories => {
                self.memory_selected = 0;
                self.update_detail();
            }
            Mode::Review => {
                self.duplicate_selected = 0;
            }
            _ => {}
        }
    }

    pub fn move_bottom(&mut self) {
        match self.mode {
            Mode::Memories => {
                self.memory_selected = self.memories.len().saturating_sub(1);
                self.update_detail();
            }
            Mode::Review => {
                self.duplicate_selected = self.duplicates.len().saturating_sub(1);
            }
            _ => {}
        }
    }

    pub fn edit_selected(&mut self) -> bool {
        if self.mode != Mode::Memories {
            return false;
        }
        let mem = match self.memories.get(self.memory_selected) {
            Some(m) => self.db.get_memory(&m.id).ok().flatten(),
            None => return false,
        };
        let mem = match mem {
            Some(m) => m,
            None => return false,
        };

        let tags_str = crate::db::format_tags(&mem.tags);
        let result = editor::open_editor(
            &mem.title,
            &mem.project,
            mem.category.as_deref().unwrap_or(""),
            &tags_str,
            mem.source.as_deref().unwrap_or(""),
            &mem.what,
            mem.why.as_deref().unwrap_or(""),
            mem.impact.as_deref().unwrap_or(""),
            mem.details.as_deref().unwrap_or(""),
        );

        if let Some(edit) = result {
            if self
                .db
                .update_memory(
                    &mem.id,
                    &edit.title,
                    &edit.what,
                    edit.why.as_deref(),
                    edit.impact.as_deref(),
                    edit.category.as_deref(),
                    &edit.tags,
                    edit.details.as_deref(),
                )
                .is_ok()
            {
                self.notify(&format!("Updated: {}", edit.title));
                self.log(&format!("Updated: {}", edit.title));
                self.refresh_memories();
                self.refresh_overview();
            }
            true
        } else {
            true // Still need to redraw
        }
    }

    pub fn new_memory(&mut self) -> bool {
        let project = if self.project_filter.is_empty() {
            "default"
        } else {
            &self.project_filter
        };
        let result = editor::open_editor("", project, "", "", "", "", "", "", "");
        if let Some(edit) = result {
            let project = if edit.project.is_empty() {
                self.project_filter.clone()
            } else {
                edit.project.clone()
            };
            let project = if project.is_empty() {
                "default".to_string()
            } else {
                project
            };
            if self
                .db
                .insert_memory(
                    &edit.title,
                    &edit.what,
                    edit.why.as_deref(),
                    edit.impact.as_deref(),
                    edit.category.as_deref(),
                    &edit.tags,
                    Some("dashboard"),
                    &project,
                    edit.details.as_deref(),
                )
                .is_ok()
            {
                self.notify(&format!("Created: {}", edit.title));
                self.log(&format!("Created: {}", edit.title));
                self.refresh_memories();
                self.refresh_overview();
            }
            true
        } else {
            true
        }
    }

    pub fn archive_selected(&mut self) {
        if let Some(mem) = self.memories.get(self.memory_selected) {
            let id = mem.id.clone();
            let title = mem.title.clone();
            let is_archived = mem.status.as_deref() == Some("archived");
            if is_archived {
                self.confirm_message = format!("Restore \"{}\"?", title);
                self.confirm_action = Some(ConfirmAction::RestoreMemory(id));
            } else {
                self.confirm_message = format!("Archive \"{}\"?", title);
                self.confirm_action = Some(ConfirmAction::ArchiveMemory(id));
            }
            self.show_confirm = true;
        }
    }

    pub fn merge_selected(&mut self) {
        if let Some(pair) = self.duplicates.get(self.duplicate_selected) {
            self.confirm_message = format!(
                "Merge \"{}\" into \"{}\"?",
                pair.right_title, pair.left_title
            );
            self.confirm_action = Some(ConfirmAction::MergePair(
                pair.left_id.clone(),
                pair.right_id.clone(),
            ));
            self.show_confirm = true;
        }
    }

    pub fn archive_duplicate_right(&mut self) {
        if let Some(pair) = self.duplicates.get(self.duplicate_selected) {
            self.confirm_message = format!("Archive \"{}\"?", pair.right_title);
            self.confirm_action = Some(ConfirmAction::ArchiveDuplicate(pair.right_id.clone()));
            self.show_confirm = true;
        }
    }

    pub fn keep_separate(&mut self) {
        if let Some(pair) = self.duplicates.get(self.duplicate_selected) {
            self.ignored_pairs
                .insert((pair.left_id.clone(), pair.right_id.clone()));
            self.duplicates.remove(self.duplicate_selected);
            if self.duplicate_selected > 0 && self.duplicate_selected >= self.duplicates.len() {
                self.duplicate_selected = self.duplicates.len().saturating_sub(1);
            }
            self.notify("Pair ignored.");
        }
    }

    pub fn execute_confirm(&mut self) {
        let action = self.confirm_action.take();
        self.show_confirm = false;
        match action {
            Some(ConfirmAction::ArchiveMemory(id)) => {
                if self.db.archive_memory(&id, "dashboard").is_ok() {
                    self.notify("Archived.");
                    self.log("Archived memory.");
                    self.refresh_memories();
                    self.refresh_overview();
                }
            }
            Some(ConfirmAction::RestoreMemory(id)) => {
                if self.db.restore_memory(&id).is_ok() {
                    self.notify("Restored.");
                    self.log("Restored memory.");
                    self.refresh_memories();
                    self.refresh_overview();
                }
            }
            Some(ConfirmAction::MergePair(keep, merge)) => {
                if self.db.merge_memories(&keep, &merge).is_ok() {
                    self.notify("Merged.");
                    self.log("Merged duplicate pair.");
                    self.refresh_memories();
                    self.refresh_overview();
                    self.refresh_duplicates();
                }
            }
            Some(ConfirmAction::ArchiveDuplicate(id)) => {
                if self.db.archive_memory(&id, "duplicate-review").is_ok() {
                    self.notify("Archived duplicate.");
                    self.log("Archived duplicate.");
                    self.refresh_memories();
                    self.refresh_overview();
                    self.refresh_duplicates();
                }
            }
            None => {}
        }
    }

    pub fn cancel_confirm(&mut self) {
        self.show_confirm = false;
        self.confirm_action = None;
    }

    pub fn dispatch_command(&mut self) {
        let cmd = self.input_buffer.trim().to_string();
        self.input_buffer.clear();
        self.input_mode = InputMode::Normal;

        let parts: Vec<&str> = cmd.splitn(2, ' ').collect();
        let cmd_name = parts
            .first()
            .map(|s| s.trim_start_matches(':'))
            .unwrap_or("");
        let arg = parts.get(1).map(|s| s.trim()).unwrap_or("");

        match cmd_name {
            "overview" | "1" => self.switch_mode(Mode::Overview),
            "memories" | "2" => self.switch_mode(Mode::Memories),
            "review" | "3" => self.switch_mode(Mode::Review),
            "ops" | "4" => self.switch_mode(Mode::Operations),
            "project" => {
                self.project_filter = arg.to_string();
                self.notify(&format!(
                    "Project: {}",
                    if arg.is_empty() { "all" } else { arg }
                ));
            }
            "q" | "quit" => self.should_quit = true,
            "refresh" => {
                self.refresh_current();
                self.notify("Refreshed.");
            }
            _ => self.notify(&format!("Unknown: {}", cmd_name)),
        }
    }

    pub fn switch_mode(&mut self, mode: Mode) {
        self.mode = mode;
        match mode {
            Mode::Overview => self.refresh_overview(),
            Mode::Memories => {
                if self.memories.is_empty() {
                    self.refresh_memories();
                }
            }
            Mode::Review => {
                if self.duplicates.is_empty() && !self.duplicate_loading {
                    self.refresh_duplicates();
                }
            }
            Mode::Operations => {}
        }
    }

    pub fn refresh_current(&mut self) {
        match self.mode {
            Mode::Overview => self.refresh_overview(),
            Mode::Memories => self.refresh_memories(),
            Mode::Review => self.refresh_duplicates(),
            Mode::Operations => {}
        }
    }

    pub fn log(&mut self, msg: &str) {
        let ts = chrono_now();
        self.op_log.push(format!("{}  {}", ts, msg));
    }
}

fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = d.as_secs();
    let hours = (secs % 86400) / 3600;
    let mins = (secs % 3600) / 60;
    let s = secs % 60;
    format!("{:02}:{:02}:{:02}", hours, mins, s)
}
