CREATE INDEX events_text ON events USING gin(to_tsvector('english',coalesce(body,''))) WHERE current AND NOT redacted;
