use memory_dashboard::mutation::{CliMutationClient, MutationClient, MutationRequest};

#[test]
#[ignore = "requires the Python memory console script and an isolated MEMORY_HOME"]
fn cli_mutation_client_writes_canonical_python_storage() {
    let executable = std::env::var("ECHOVAULT_TEST_MEMORY_EXECUTABLE").unwrap();
    let memory_home = std::env::var("MEMORY_HOME").unwrap();
    let client = CliMutationClient {
        executable,
        memory_home,
    };

    let created = client
        .apply(&MutationRequest::Create {
            title: "Rust bridge".to_string(),
            what: "created by rust".to_string(),
            why: None,
            impact: None,
            category: Some("decision".to_string()),
            tags: vec!["bridge".to_string()],
            source: Some("dashboard".to_string()),
            project: "rust-bridge--111111111111".to_string(),
            details: Some("Canonical cross-language details".to_string()),
            actor: "dashboard".to_string(),
        })
        .unwrap();
    assert_eq!(created.status, "created");

    let updated = client
        .apply(&MutationRequest::Update {
            memory_id: created.memory_id.clone(),
            title: "Rust bridge".to_string(),
            what: "updated by rust".to_string(),
            why: None,
            impact: Some("canonical storage".to_string()),
            category: Some("decision".to_string()),
            tags: vec!["bridge".to_string(), "updated".to_string()],
            details: Some("Canonical cross-language details".to_string()),
            actor: "dashboard".to_string(),
        })
        .unwrap();
    assert_eq!(updated.status, "updated");
    assert_eq!(updated.memory_id, created.memory_id);
}
