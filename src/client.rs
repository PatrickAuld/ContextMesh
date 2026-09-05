use crate::{db, events::Insert};
use serde_json::Value;
use std::path::{Path, PathBuf};

pub struct Client {
    http: reqwest::Client,
    url: String,
    token: String,
    outbox: Option<PathBuf>,
}

#[derive(Debug, Default, serde::Serialize)]
pub struct Flush {
    pub sent: usize,
    pub rejected: usize,
    pub pending: usize,
}

impl Client {
    pub fn new(url: &str, token: &str) -> anyhow::Result<Self> {
        Ok(Self {
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(240))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            url: url.trim_end_matches('/').to_owned(),
            token: token.to_owned(),
            outbox: None,
        })
    }
    pub fn with_outbox(mut self, root: &Path) -> anyhow::Result<Self> {
        let identity = db::hash(&format!("{}\n{}", self.url, self.token));
        let path = root.join(identity);
        std::fs::create_dir_all(&path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
        }
        self.outbox = Some(path);
        Ok(self)
    }
    pub async fn remember(&self, event: &Insert) -> anyhow::Result<Value> {
        if let Some(outbox) = &self.outbox {
            use std::io::Write;
            let bytes = serde_json::to_vec(event)?;
            let key = db::hash(&String::from_utf8_lossy(&bytes));
            let temp = outbox.join(format!("{}.tmp", uuid::Uuid::new_v4()));
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options.open(&temp)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            std::fs::rename(&temp, outbox.join(format!("{key}.pending")))?;
            #[cfg(unix)]
            std::fs::File::open(outbox)?.sync_all()?;
            let flushed = self.flush().await?;
            return Ok(serde_json::to_value(flushed)?);
        }
        self.request("/v1/events", &serde_json::to_value(event)?)
            .await
    }
    pub async fn extract(&self, query: Value) -> anyhow::Result<Value> {
        self.request("/v1/query", &query).await
    }
    async fn request(&self, path: &str, body: &Value) -> anyhow::Result<Value> {
        let response = self
            .http
            .post(format!("{}{}", self.url, path))
            .bearer_auth(&self.token)
            .json(body)
            .send()
            .await?;
        let status = response.status();
        anyhow::ensure!(status.is_success(), "ContextMesh HTTP {status}");
        Ok(response.json().await?)
    }
    pub async fn flush(&self) -> anyhow::Result<Flush> {
        let mut summary = Flush::default();
        let Some(outbox) = &self.outbox else {
            return Ok(summary);
        };
        let mut entries = std::fs::read_dir(outbox)?
            .filter_map(std::result::Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|e| e == "pending"))
            .collect::<Vec<_>>();
        entries.sort_by_cached_key(|path| {
            std::fs::read(path)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Insert>(&bytes).ok())
                .map(|event| (event.source, event.external_id, event.revision))
        });
        let total = entries.len();
        for (index, path) in entries.into_iter().enumerate() {
            if index >= 100 {
                summary.pending += total - index;
                break;
            }
            let bytes = match std::fs::read(&path) {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => return Err(e.into()),
            };
            let response = self
                .http
                .post(format!("{}/v1/events", self.url))
                .bearer_auth(&self.token)
                .header("content-type", "application/json")
                .timeout(std::time::Duration::from_secs(10))
                .body(bytes)
                .send()
                .await;
            match response {
                Ok(r) if r.status().is_success() => {
                    let _ = std::fs::remove_file(&path);
                    summary.sent += 1;
                }
                Ok(r) if matches!(r.status().as_u16(), 400 | 403 | 404 | 409 | 410 | 413 | 422) => {
                    let rejected = path.with_extension("rejected");
                    match std::fs::rename(&path, rejected) {
                        Ok(()) => {}
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                        Err(e) => return Err(e.into()),
                    }
                    summary.rejected += 1;
                }
                _ => {
                    summary.pending += total - index;
                    break;
                }
            }
        }
        Ok(summary)
    }
}
