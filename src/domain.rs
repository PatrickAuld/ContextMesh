use crate::error::{Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphConfig {
    #[serde(default = "mode")]
    pub mode: String,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub instructions: String,
    #[serde(default)]
    pub temperature: f32,
    #[serde(default = "extractor")]
    pub extractor_version: String,
}
fn mode() -> String {
    "literal".into()
}
fn extractor() -> String {
    "claims-v1".into()
}
impl GraphConfig {
    pub fn validate(&self) -> Result<()> {
        if !["literal", "llm"].contains(&self.mode.as_str())
            || !(0.0..=2.0).contains(&self.temperature)
            || self.instructions.len() > 16000
            || self.extractor_version != "claims-v1"
        {
            return Err(Error::bad("invalid_graph_config"));
        }
        Ok(())
    }
}
impl Default for GraphConfig {
    fn default() -> Self {
        Self {
            mode: mode(),
            model: None,
            instructions: String::new(),
            temperature: 0.0,
            extractor_version: extractor(),
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Claim {
    pub text: String,
    pub quote: String,
    #[serde(default = "intent")]
    pub intent: String,
    #[serde(default)]
    pub entities: Vec<String>,
    #[serde(default)]
    pub applies: BTreeMap<String, Value>,
    #[serde(default)]
    pub dependencies: BTreeMap<String, String>,
    #[serde(default)]
    pub slot: Option<String>,
    #[serde(default)]
    pub relations: Vec<Relation>,
}
fn intent() -> String {
    "observation".into()
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Relation {
    pub from: String,
    pub relation: String,
    pub to: String,
}
impl Claim {
    pub fn validate(&self, source: &str) -> Result<()> {
        if self.text.is_empty()
            || self.text.len() > 16000
            || self.quote.is_empty()
            || !source.contains(&self.quote)
            || self.quote.len() > 16000
            || self.entities.len() > 32
            || self.relations.len() > 64
            || !["observation", "guidance", "evidence"].contains(&self.intent.as_str())
        {
            return Err(Error::bad("invalid_claim"));
        }
        if self.entities.iter().any(|e| e.is_empty() || e.len() > 256)
            || self.relations.iter().any(|r| {
                !self.entities.contains(&r.from)
                    || !self.entities.contains(&r.to)
                    || r.relation.len() > 128
            })
        {
            return Err(Error::bad("invalid_entities"));
        }
        Ok(())
    }
}
