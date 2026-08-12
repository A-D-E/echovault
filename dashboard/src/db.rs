use crate::mutation::{CliMutationClient, MutationClient, MutationRequest, MutationResponse};
use rusqlite::{params, Connection, OpenFlags, Result};
use std::io;
use std::path::Path;

pub struct Memory {
    pub id: String,
    pub title: String,
    pub what: String,
    pub why: Option<String>,
    pub impact: Option<String>,
    pub tags: Option<String>,
    pub category: Option<String>,
    pub project: String,
    pub source: Option<String>,
    pub status: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub details: Option<String>,
    pub superseded_by: Option<String>,
}

#[derive(Clone)]
pub struct DuplicatePair {
    pub left_id: String,
    pub left_title: String,
    pub left_what: String,
    pub right_id: String,
    pub right_title: String,
    pub right_what: String,
    pub project: String,
    pub score: f64,
}

pub struct Stats {
    pub total: i64,
    pub active: i64,
    pub archived: i64,
    pub projects: Vec<(String, i64)>,
    pub categories: Vec<(String, i64)>,
    pub recent: Vec<Memory>,
}

pub struct Db {
    conn: Connection,
    mutations: Box<dyn MutationClient>,
}

impl Db {
    pub fn open(db_path: &Path) -> Result<Self> {
        let memory_home = db_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_string_lossy()
            .into_owned();
        let executable =
            std::env::var("ECHOVAULT_MEMORY_EXECUTABLE").unwrap_or_else(|_| "memory".to_string());
        Self::open_with_mutations(
            db_path,
            Box::new(CliMutationClient {
                executable,
                memory_home,
            }),
        )
    }

    pub fn open_with_mutations(db_path: &Path, mutations: Box<dyn MutationClient>) -> Result<Self> {
        let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        Ok(Db { conn, mutations })
    }

    pub fn get_stats(&self, project: Option<&str>) -> Result<Stats> {
        let (where_clause, project_param) = if let Some(p) = project {
            ("WHERE project = ?1", Some(p.to_string()))
        } else {
            ("", None)
        };

        // Totals
        let sql = format!(
            "SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IS NULL OR status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) AS archived
            FROM memories {}",
            where_clause
        );
        let (total, active, archived) = if let Some(ref p) = project_param {
            self.conn.query_row(&sql, params![p], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })?
        } else {
            self.conn.query_row(&sql, [], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })?
        };

        // Projects
        let sql = format!(
            "SELECT project, COUNT(*) AS count FROM memories {} GROUP BY project ORDER BY count DESC, project ASC",
            where_clause
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let projects: Vec<(String, i64)> = if let Some(ref p) = project_param {
            stmt.query_map(params![p], |row| Ok((row.get(0)?, row.get(1)?)))?
                .filter_map(|r| r.ok())
                .collect()
        } else {
            stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                .filter_map(|r| r.ok())
                .collect()
        };

        // Categories (active only)
        let cat_where = if let Some(ref p) = project_param {
            format!(
                "WHERE (m.status IS NULL OR m.status = 'active') AND m.project = '{}'",
                p.replace('\'', "''")
            )
        } else {
            "WHERE (m.status IS NULL OR m.status = 'active')".to_string()
        };
        let sql = format!(
            "SELECT COALESCE(m.category, 'uncategorized') AS cat, COUNT(*) AS count
            FROM memories m {} GROUP BY cat ORDER BY count DESC",
            cat_where
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let categories: Vec<(String, i64)> = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .filter_map(|r| r.ok())
            .collect();

        // Recent
        let recent = self.list_recent(10, project)?;

        Ok(Stats {
            total,
            active,
            archived,
            projects,
            categories,
            recent,
        })
    }

    pub fn list_recent(&self, limit: usize, project: Option<&str>) -> Result<Vec<Memory>> {
        let (where_clause, project_param) = if let Some(p) = project {
            (
                "WHERE (m.status IS NULL OR m.status = 'active') AND m.project = ?1",
                Some(p.to_string()),
            )
        } else {
            ("WHERE (m.status IS NULL OR m.status = 'active')", None)
        };

        let sql = format!(
            "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                    m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                    d.body as details
            FROM memories m
            LEFT JOIN memory_details d ON d.memory_id = m.id
            {} ORDER BY m.created_at DESC LIMIT ?{}",
            where_clause,
            if project_param.is_some() { "2" } else { "1" }
        );

        let mut stmt = self.conn.prepare(&sql)?;
        let rows = if let Some(ref p) = project_param {
            stmt.query_map(params![p, limit as i64], |row| row_to_memory(row))?
                .filter_map(|r| r.ok())
                .collect()
        } else {
            stmt.query_map(params![limit as i64], |row| row_to_memory(row))?
                .filter_map(|r| r.ok())
                .collect()
        };
        Ok(rows)
    }

    pub fn list_memories(
        &self,
        limit: usize,
        project: Option<&str>,
        category: Option<&str>,
        include_archived: bool,
        query: Option<&str>,
    ) -> Result<Vec<Memory>> {
        let mut conditions = Vec::new();
        let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();
        let mut param_idx = 1;

        if !include_archived {
            conditions.push("(m.status IS NULL OR m.status = 'active')".to_string());
        }
        if let Some(p) = project {
            conditions.push(format!("m.project = ?{}", param_idx));
            param_values.push(Box::new(p.to_string()));
            param_idx += 1;
        }
        if let Some(c) = category {
            conditions.push(format!("m.category = ?{}", param_idx));
            param_values.push(Box::new(c.to_string()));
            param_idx += 1;
        }

        let where_clause = if conditions.is_empty() {
            String::new()
        } else {
            format!("WHERE {}", conditions.join(" AND "))
        };

        // If there's a text query, use FTS
        let sql = if let Some(q) = query {
            let fts_query = build_fts_query(q);
            if fts_query.is_empty() {
                format!(
                    "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                            m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                            d.body as details
                    FROM memories m
                    LEFT JOIN memory_details d ON d.memory_id = m.id
                    {} ORDER BY m.updated_at DESC, m.created_at DESC LIMIT ?{}",
                    where_clause, param_idx
                )
            } else {
                conditions.push(format!(
                    "m.rowid IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?{})",
                    param_idx
                ));
                param_values.push(Box::new(fts_query));
                param_idx += 1;
                let where_clause = format!("WHERE {}", conditions.join(" AND "));
                format!(
                    "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                            m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                            d.body as details
                    FROM memories m
                    LEFT JOIN memory_details d ON d.memory_id = m.id
                    {} ORDER BY m.updated_at DESC, m.created_at DESC LIMIT ?{}",
                    where_clause, param_idx
                )
            }
        } else {
            format!(
                "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                        m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                        d.body as details
                FROM memories m
                LEFT JOIN memory_details d ON d.memory_id = m.id
                {} ORDER BY m.updated_at DESC, m.created_at DESC LIMIT ?{}",
                where_clause, param_idx
            )
        };

        param_values.push(Box::new(limit as i64));

        let mut stmt = self.conn.prepare(&sql)?;
        let params_ref: Vec<&dyn rusqlite::types::ToSql> =
            param_values.iter().map(|p| p.as_ref()).collect();
        let rows = stmt
            .query_map(params_ref.as_slice(), |row| row_to_memory(row))?
            .filter_map(|r| r.ok())
            .collect();
        Ok(rows)
    }

    pub fn get_memory(&self, id: &str) -> Result<Option<Memory>> {
        let sql = "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                          m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                          d.body as details
                   FROM memories m
                   LEFT JOIN memory_details d ON d.memory_id = m.id
                   WHERE m.id = ?1";
        let mut stmt = self.conn.prepare(sql)?;
        let mut rows = stmt.query_map(params![id], |row| row_to_memory(row))?;
        Ok(rows.next().and_then(|r| r.ok()))
    }

    pub fn archive_memory(&self, id: &str, reason: &str) -> Result<()> {
        self.apply_expected(
            &MutationRequest::Archive {
                memory_id: id.to_string(),
                reason: reason.to_string(),
                actor: "dashboard".to_string(),
            },
            id,
            &["archived"],
        )?;
        Ok(())
    }

    pub fn restore_memory(&self, id: &str) -> Result<()> {
        self.apply_expected(
            &MutationRequest::Restore {
                memory_id: id.to_string(),
                actor: "dashboard".to_string(),
            },
            id,
            &["restored"],
        )?;
        Ok(())
    }

    pub fn merge_memories(&self, keep_id: &str, merge_id: &str) -> Result<()> {
        self.apply_expected(
            &MutationRequest::Merge {
                canonical_id: keep_id.to_string(),
                source_ids: vec![merge_id.to_string()],
                actor: "dashboard".to_string(),
            },
            keep_id,
            &["merged"],
        )?;
        Ok(())
    }

    pub fn update_memory(
        &self,
        id: &str,
        title: &str,
        what: &str,
        why: Option<&str>,
        impact: Option<&str>,
        category: Option<&str>,
        tags: &[String],
        details: Option<&str>,
    ) -> Result<()> {
        self.apply_expected(
            &MutationRequest::Update {
                memory_id: id.to_string(),
                title: title.to_string(),
                what: what.to_string(),
                why: why.map(str::to_string),
                impact: impact.map(str::to_string),
                category: category.map(str::to_string),
                tags: tags.to_vec(),
                details: details.map(str::to_string),
                actor: "dashboard".to_string(),
            },
            id,
            &["updated"],
        )?;
        Ok(())
    }

    pub fn insert_memory(
        &self,
        title: &str,
        what: &str,
        why: Option<&str>,
        impact: Option<&str>,
        category: Option<&str>,
        tags: &[String],
        source: Option<&str>,
        project: &str,
        details: Option<&str>,
    ) -> Result<String> {
        let response = self.mutations.apply(&MutationRequest::Create {
            title: title.to_string(),
            what: what.to_string(),
            why: why.map(str::to_string),
            impact: impact.map(str::to_string),
            category: category.map(str::to_string),
            tags: tags.to_vec(),
            source: source.map(str::to_string),
            project: project.to_string(),
            details: details.map(str::to_string),
            actor: "dashboard".to_string(),
        })?;
        if !matches!(response.status.as_str(), "created" | "updated")
            || response.memory_id.is_empty()
        {
            return Err(invalid_mutation_response());
        }
        Ok(response.memory_id)
    }

    pub fn delete_memory(&self, id: &str) -> Result<()> {
        self.apply_expected(
            &MutationRequest::Delete {
                memory_id: id.to_string(),
                actor: "dashboard".to_string(),
            },
            id,
            &["deleted"],
        )?;
        Ok(())
    }

    fn apply_expected(
        &self,
        request: &MutationRequest,
        expected_id: &str,
        expected_statuses: &[&str],
    ) -> Result<MutationResponse> {
        let response = self.mutations.apply(request)?;
        if response.memory_id != expected_id
            || !expected_statuses.contains(&response.status.as_str())
        {
            return Err(invalid_mutation_response());
        }
        Ok(response)
    }

    /// Get all memories for duplicate detection (lightweight, no details)
    pub fn list_for_duplicates(&self, project: Option<&str>) -> Result<Vec<Memory>> {
        let (where_clause, project_param) = if let Some(p) = project {
            (
                "WHERE (m.status IS NULL OR m.status = 'active') AND m.project = ?1",
                Some(p.to_string()),
            )
        } else {
            ("WHERE (m.status IS NULL OR m.status = 'active')", None)
        };

        let sql = format!(
            "SELECT m.id, m.title, m.what, m.why, m.impact, m.tags, m.category,
                    m.project, m.source, m.status, m.created_at, m.updated_at, m.superseded_by,
                    NULL as details
            FROM memories m
            {} ORDER BY m.updated_at DESC LIMIT 500",
            where_clause
        );

        let mut stmt = self.conn.prepare(&sql)?;
        let rows = if let Some(ref p) = project_param {
            stmt.query_map(params![p], |row| row_to_memory(row))?
                .filter_map(|r| r.ok())
                .collect()
        } else {
            stmt.query_map([], |row| row_to_memory(row))?
                .filter_map(|r| r.ok())
                .collect()
        };
        Ok(rows)
    }
}

fn invalid_mutation_response() -> rusqlite::Error {
    rusqlite::Error::ToSqlConversionFailure(Box::new(io::Error::new(
        io::ErrorKind::InvalidData,
        "canonical mutation returned an unexpected response",
    )))
}

fn row_to_memory(row: &rusqlite::Row) -> Result<Memory> {
    Ok(Memory {
        id: row.get(0)?,
        title: row.get(1)?,
        what: row.get(2)?,
        why: row.get(3)?,
        impact: row.get(4)?,
        tags: row.get(5)?,
        category: row.get(6)?,
        project: row.get(7)?,
        source: row.get(8)?,
        status: row.get(9)?,
        created_at: row.get(10)?,
        updated_at: row.get(11)?,
        superseded_by: row.get(12)?,
        details: row.get(13)?,
    })
}

pub fn format_tags(tags: &Option<String>) -> String {
    parse_tags(tags).join(", ")
}

fn parse_tags(tags: &Option<String>) -> Vec<String> {
    match tags {
        Some(t) => {
            let t = t.trim();
            if t.starts_with('[') {
                t.trim_matches(|c| c == '[' || c == ']')
                    .split(',')
                    .map(|s| s.trim().trim_matches('"').to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            } else {
                t.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            }
        }
        None => Vec::new(),
    }
}

const STOPWORDS: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "has", "have", "he",
    "in", "is", "it", "its", "my", "no", "not", "of", "on", "or", "she", "so", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "to", "was", "we", "were",
    "will", "with",
];

fn build_fts_query(query: &str) -> String {
    let terms: Vec<&str> = query
        .split_whitespace()
        .filter(|t| t.len() >= 2)
        .filter(|t| !STOPWORDS.contains(&t.to_lowercase().as_str()))
        .collect();
    if terms.is_empty() {
        return query
            .split_whitespace()
            .filter(|t| t.len() >= 2)
            .map(|t| format!("\"{}\"*", t))
            .collect::<Vec<_>>()
            .join(" OR ");
    }
    terms
        .iter()
        .map(|t| format!("\"{}\"*", t))
        .collect::<Vec<_>>()
        .join(" OR ")
}
