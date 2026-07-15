use serde::{Deserialize, Serialize};
use std::io::{self, Write};
use std::process::{Child, Command, Stdio};

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum MutationRequest {
    Create {
        title: String,
        what: String,
        why: Option<String>,
        impact: Option<String>,
        category: Option<String>,
        tags: Vec<String>,
        source: Option<String>,
        project: String,
        details: Option<String>,
        actor: String,
    },
    Update {
        memory_id: String,
        title: String,
        what: String,
        why: Option<String>,
        impact: Option<String>,
        category: Option<String>,
        tags: Vec<String>,
        details: Option<String>,
        actor: String,
    },
    Archive {
        memory_id: String,
        reason: String,
        actor: String,
    },
    Restore {
        memory_id: String,
        actor: String,
    },
    Merge {
        canonical_id: String,
        source_ids: Vec<String>,
        actor: String,
    },
    Delete {
        memory_id: String,
        actor: String,
    },
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MutationResponse {
    pub status: String,
    pub memory_id: String,
}

pub trait MutationClient {
    fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse>;
}

pub struct CliMutationClient {
    pub executable: String,
    pub memory_home: String,
}

fn bridge_error(message: impl Into<String>) -> rusqlite::Error {
    rusqlite::Error::ToSqlConversionFailure(Box::new(io::Error::other(message.into())))
}

fn terminate_and_reap(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

impl MutationClient for CliMutationClient {
    fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse> {
        let payload = serde_json::to_vec(request)
            .map_err(|error| bridge_error(format!("mutation serialization failed: {error}")))?;
        let mut child = Command::new(&self.executable)
            .args(["admin", "apply", "--json-stdin"])
            .env("MEMORY_HOME", &self.memory_home)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| {
                bridge_error(format!("canonical mutation could not start: {error}"))
            })?;
        let mut stdin = match child.stdin.take() {
            Some(stdin) => stdin,
            None => {
                terminate_and_reap(&mut child);
                return Err(bridge_error("canonical mutation stdin was unavailable"));
            }
        };
        if let Err(error) = stdin.write_all(&payload) {
            drop(stdin);
            terminate_and_reap(&mut child);
            return Err(bridge_error(format!(
                "canonical mutation input failed: {error}"
            )));
        }
        drop(stdin);

        let output = child
            .wait_with_output()
            .map_err(|error| bridge_error(format!("canonical mutation wait failed: {error}")))?;
        if !output.status.success() {
            return Err(bridge_error(format!(
                "canonical mutation failed with status {}",
                output.status
            )));
        }
        serde_json::from_slice(&output.stdout)
            .map_err(|error| bridge_error(format!("invalid canonical mutation response: {error}")))
    }
}

#[cfg(test)]
mod tests {
    use super::{CliMutationClient, MutationClient, MutationRequest, MutationResponse};
    use crate::db::Db;
    use rusqlite::Connection;
    use std::sync::{Arc, Mutex};
    use tempfile::NamedTempFile;

    #[derive(Clone)]
    struct RecordingMutationClient {
        requests: Arc<Mutex<Vec<MutationRequest>>>,
        response: MutationResponse,
    }

    impl RecordingMutationClient {
        fn returning(status: &str, memory_id: &str) -> Self {
            Self {
                requests: Arc::new(Mutex::new(Vec::new())),
                response: MutationResponse {
                    status: status.to_string(),
                    memory_id: memory_id.to_string(),
                },
            }
        }

        fn requests(&self) -> Vec<MutationRequest> {
            self.requests.lock().unwrap().clone()
        }
    }

    impl MutationClient for RecordingMutationClient {
        fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse> {
            self.requests.lock().unwrap().push(request.clone());
            Ok(self.response.clone())
        }
    }

    fn fixture_db() -> NamedTempFile {
        let file = NamedTempFile::new().unwrap();
        let connection = Connection::open(file.path()).unwrap();
        connection
            .execute_batch(
                "
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    what TEXT NOT NULL,
                    why TEXT,
                    impact TEXT,
                    tags TEXT,
                    category TEXT,
                    project TEXT NOT NULL,
                    source TEXT,
                    status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    superseded_by TEXT
                );
                CREATE TABLE memory_details (
                    memory_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL
                );
                ",
            )
            .unwrap();
        file
    }

    #[test]
    fn archive_is_sent_to_canonical_cli() {
        let fixture = fixture_db();
        let fake = RecordingMutationClient::returning("archived", "memory-1");
        let db = Db::open_with_mutations(fixture.path(), Box::new(fake.clone())).unwrap();
        db.archive_memory("memory-1", "dashboard").unwrap();
        assert_eq!(
            fake.requests(),
            vec![MutationRequest::Archive {
                memory_id: "memory-1".to_string(),
                reason: "dashboard".to_string(),
                actor: "dashboard".to_string(),
            }]
        );
    }

    #[test]
    fn create_uses_the_canonical_id_returned_by_python_without_local_insert() {
        let fixture = fixture_db();
        let fake =
            RecordingMutationClient::returning("created", "11111111-1111-4111-8111-111111111111");
        let db = Db::open_with_mutations(fixture.path(), Box::new(fake.clone())).unwrap();
        let memory_id = db
            .insert_memory(
                "Title",
                "What",
                None,
                None,
                None,
                &[],
                Some("dashboard"),
                "project--111111111111",
                None,
            )
            .unwrap();
        assert_eq!(memory_id, "11111111-1111-4111-8111-111111111111");
        assert!(matches!(fake.requests()[0], MutationRequest::Create { .. }));
        let verifier = Connection::open(fixture.path()).unwrap();
        let count: i64 = verifier
            .query_row("SELECT COUNT(*) FROM memories", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn update_restore_merge_and_delete_are_typed_requests() {
        let fixture = fixture_db();

        let update = RecordingMutationClient::returning("updated", "memory-1");
        let db = Db::open_with_mutations(fixture.path(), Box::new(update.clone())).unwrap();
        db.update_memory(
            "memory-1",
            "Renamed",
            "Updated",
            None,
            Some("impact"),
            Some("decision"),
            &["one".to_string()],
            Some("details"),
        )
        .unwrap();
        assert!(matches!(
            update.requests()[0],
            MutationRequest::Update { .. }
        ));
        drop(db);

        let restore = RecordingMutationClient::returning("restored", "memory-1");
        let db = Db::open_with_mutations(fixture.path(), Box::new(restore.clone())).unwrap();
        db.restore_memory("memory-1").unwrap();
        assert_eq!(
            restore.requests(),
            vec![MutationRequest::Restore {
                memory_id: "memory-1".to_string(),
                actor: "dashboard".to_string(),
            }]
        );
        drop(db);

        let merge = RecordingMutationClient::returning("merged", "memory-1");
        let db = Db::open_with_mutations(fixture.path(), Box::new(merge.clone())).unwrap();
        db.merge_memories("memory-1", "memory-2").unwrap();
        assert_eq!(
            merge.requests(),
            vec![MutationRequest::Merge {
                canonical_id: "memory-1".to_string(),
                source_ids: vec!["memory-2".to_string()],
                actor: "dashboard".to_string(),
            }]
        );
        drop(db);

        let delete = RecordingMutationClient::returning("deleted", "memory-2");
        let db = Db::open_with_mutations(fixture.path(), Box::new(delete.clone())).unwrap();
        db.delete_memory("memory-2").unwrap();
        assert_eq!(
            delete.requests(),
            vec![MutationRequest::Delete {
                memory_id: "memory-2".to_string(),
                actor: "dashboard".to_string(),
            }]
        );
    }

    #[test]
    fn mismatched_response_identity_is_rejected() {
        let fixture = fixture_db();
        let fake = RecordingMutationClient::returning("archived", "different-memory");
        let db = Db::open_with_mutations(fixture.path(), Box::new(fake)).unwrap();
        assert!(db.archive_memory("memory-1", "dashboard").is_err());
    }

    #[test]
    fn mismatched_response_status_is_rejected() {
        let fixture = fixture_db();
        let fake = RecordingMutationClient::returning("restored", "memory-1");
        let db = Db::open_with_mutations(fixture.path(), Box::new(fake)).unwrap();
        assert!(db.archive_memory("memory-1", "dashboard").is_err());
    }

    #[test]
    fn canonical_duplicate_create_may_return_updated() {
        let fixture = fixture_db();
        let fake =
            RecordingMutationClient::returning("updated", "11111111-1111-4111-8111-111111111111");
        let db = Db::open_with_mutations(fixture.path(), Box::new(fake)).unwrap();
        assert_eq!(
            db.insert_memory(
                "Title",
                "What",
                None,
                None,
                None,
                &[],
                Some("dashboard"),
                "project--111111111111",
                None,
            )
            .unwrap(),
            "11111111-1111-4111-8111-111111111111"
        );
    }

    #[test]
    fn shared_golden_requests_round_trip_without_schema_drift() {
        let cases: Vec<serde_json::Value> = serde_json::from_str(include_str!(
            "../../tests/fixtures/dashboard-mutations-v1.json"
        ))
        .unwrap();
        for case in cases {
            let request = case.get("request").unwrap().clone();
            let parsed: MutationRequest = serde_json::from_value(request.clone()).unwrap();
            assert_eq!(serde_json::to_value(parsed).unwrap(), request);
        }
    }

    #[test]
    fn rust_contract_rejects_unknown_request_fields() {
        let request = serde_json::json!({
            "action": "delete",
            "memory_id": "22222222-2222-4222-8222-222222222222",
            "actor": "dashboard",
            "command": "ignored"
        });
        assert!(serde_json::from_value::<MutationRequest>(request).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn broken_bridge_stdin_does_not_leave_the_child_running() {
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use std::process::{Command, Stdio};

        let directory = tempfile::tempdir().unwrap();
        let executable = directory.path().join("broken-memory");
        let pid_file = directory.path().join("child.pid");
        fs::write(
            &executable,
            format!(
                "#!/bin/sh\nprintf '%s' \"$$\" > '{}'\nexec 0<&-\nsleep 30\n",
                pid_file.display()
            ),
        )
        .unwrap();
        let mut permissions = fs::metadata(&executable).unwrap().permissions();
        permissions.set_mode(0o700);
        fs::set_permissions(&executable, permissions).unwrap();

        let client = CliMutationClient {
            executable: executable.to_string_lossy().into_owned(),
            memory_home: directory.path().to_string_lossy().into_owned(),
        };
        let result = client.apply(&MutationRequest::Create {
            title: "Broken bridge".to_string(),
            what: "x".repeat(2 * 1024 * 1024),
            why: None,
            impact: None,
            category: None,
            tags: vec![],
            source: Some("dashboard".to_string()),
            project: "project--111111111111".to_string(),
            details: None,
            actor: "dashboard".to_string(),
        });
        assert!(result.is_err());

        let pid = fs::read_to_string(pid_file).unwrap();
        let alive = Command::new("kill")
            .args(["-0", pid.trim()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success();
        if alive {
            let _ = Command::new("kill")
                .args(["-9", pid.trim()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        assert!(
            !alive,
            "bridge child must be killed and reaped on stdin failure"
        );
    }
}
