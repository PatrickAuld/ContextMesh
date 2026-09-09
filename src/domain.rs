use crate::error::{Error, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;
use uuid::Uuid;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Visibility {
    #[default]
    Personal,
    Internal,
    Restricted,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scope {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project: Option<String>,
    #[serde(default)]
    pub visibility: Visibility,
    #[serde(default)]
    pub groups: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScopeFilter {
    #[serde(default)]
    pub conversation: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Support {
    pub record_id: Uuid,
    pub quote: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Derivation {
    pub model: String,
    pub extractor_version: String,
    pub instructions: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewRecord {
    pub id: Uuid,
    pub content: String,
    #[serde(default)]
    pub scope: Scope,
    #[serde(default)]
    pub inputs: Vec<Uuid>,
    #[serde(default)]
    pub supports: Vec<Support>,
    #[serde(default)]
    pub supersedes: Vec<Uuid>,
    #[serde(default = "empty_object")]
    pub metadata: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Record {
    pub id: Uuid,
    pub content: String,
    pub scope: Scope,
    pub inputs: Vec<Uuid>,
    pub supports: Vec<Support>,
    pub supersedes: Vec<Uuid>,
    pub metadata: Value,
    pub author: String,
    pub agent_id: Option<Uuid>,
    pub recorded_at: DateTime<Utc>,
    pub sequence: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub derivation: Option<Derivation>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AppendRecord {
    pub id: Uuid,
    pub duplicate: bool,
}
#[derive(Clone, Debug, Serialize)]
pub struct AppendResult {
    pub records: Vec<AppendRecord>,
}

fn empty_object() -> Value {
    Value::Object(Map::new())
}

impl Scope {
    pub fn validate(&mut self) -> Result<()> {
        self.groups.sort();
        self.groups.dedup();
        if self
            .conversation
            .as_ref()
            .is_some_and(|v| v.is_empty() || v.len() > 512)
            || self
                .project
                .as_ref()
                .is_some_and(|v| v.is_empty() || v.len() > 512)
            || self.groups.len() > 64
            || self.groups.iter().any(|g| g.is_empty() || g.len() > 256)
            || matches!(self.visibility, Visibility::Personal) && !self.groups.is_empty()
        {
            return Err(Error::bad("invalid_scope"));
        }
        Ok(())
    }
    pub fn readable_by(&self, subject: &str, author: &str, groups: &[String], admin: bool) -> bool {
        match self.visibility {
            Visibility::Personal => subject == author || admin,
            Visibility::Internal => true,
            Visibility::Restricted => admin || self.groups.iter().any(|g| groups.contains(g)),
        }
    }
}

impl NewRecord {
    pub fn validate(&mut self) -> Result<()> {
        self.scope.validate()?;
        let unique_inputs: HashSet<_> = self.inputs.iter().collect();
        let unique_supersedes: HashSet<_> = self.supersedes.iter().collect();
        if self.content.is_empty()
            || self.content.len() > 256_000
            || self.inputs.len() > 256
            || self.supports.len() > 256
            || self.supersedes.len() > 256
            || self
                .supports
                .iter()
                .any(|s| s.quote.is_empty() || s.quote.len() > 32_000)
            || !self.metadata.is_object()
            || serde_json::to_vec(&self.metadata).map_or(true, |v| v.len() > 64_000)
            || unique_inputs.len() != self.inputs.len()
            || unique_supersedes.len() != self.supersedes.len()
        {
            return Err(Error::bad("invalid_record"));
        }
        if self
            .supports
            .iter()
            .any(|s| !self.inputs.contains(&s.record_id))
            || self.supersedes.iter().any(|id| !self.inputs.contains(id))
            || self.inputs.contains(&self.id)
        {
            return Err(Error::bad("invalid_lineage"));
        }
        Ok(())
    }
}

impl Derivation {
    pub fn validate(&self) -> Result<()> {
        if self.model.is_empty()
            || self.model.len() > 512
            || self.extractor_version.is_empty()
            || self.extractor_version.len() > 256
            || self.instructions.len() > 32_000
        {
            return Err(Error::bad("invalid_derivation"));
        }
        Ok(())
    }
}
