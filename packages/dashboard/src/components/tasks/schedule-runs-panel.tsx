import React from 'react';
import { Button, Paper, TextField } from '@mui/material';

interface LoopSummary {
  task_id: string;
  status?: string | null;
  unix_millis_start_time?: number | null;
  unix_millis_finish_time?: number | null;
}

interface ScheduleRun {
  schedule_id: number;
  start_from?: string | null;
  until?: string | null;
  planned_end_at?: string | null;
  loops: LoopSummary[];
}

export const ScheduleRunsPanel: React.FC = () => {
  const apiBase =
    (window as any).__RMF_API_BASE__ || process.env.REACT_APP_API_BASE || 'http://127.0.0.1:8000';
  const [taskId, setTaskId] = React.useState<string>('');
  const [runs, setRuns] = React.useState<ScheduleRun[] | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = async (tid: number) => {
    try {
      setLoading(true);
      setError(null);
      setRuns(null);
      const resp = await fetch(`${apiBase}/scheduled_tasks/${tid}/runs`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as ScheduleRun[];
      setRuns(data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const backfill = async (tid: number) => {
    try {
      setLoading(true);
      setError(null);
      const resp = await fetch(`${apiBase}/scheduled_tasks/${tid}/backfill_labels`, {
        method: 'POST',
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await load(tid);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const getStatusColor = (status?: string | null): string => {
    switch (status) {
      case 'completed':
        return '#2e7d32';
      case 'failed':
        return '#c62828';
      case 'cancelled':
        return '#f57c00';
      case 'queued':
        return '#1565c0';
      case 'underway':
        return '#0277bd';
      default:
        return '#666';
    }
  };

  return (
    <Paper elevation={2} style={{ marginTop: 16, padding: 24, backgroundColor: '#fafafa' }}>
      {/* Header Section */}
      <div style={{ marginBottom: 24 }}>
        <h3 style={{ margin: '0 0 12px 0', color: '#333' }}>Task Runs</h3>
        <div
          style={{
            display: 'flex',
            gap: 12,
            alignItems: 'flex-end',
            flexWrap: 'wrap',
          }}
        >
          <TextField
            size="small"
            label="Scheduled Task ID"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            inputProps={{ inputMode: 'numeric', pattern: '[0-9]*' }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                const n = Number(taskId);
                if (!Number.isNaN(n) && n > 0) load(n);
              }
            }}
            style={{ minWidth: 200 }}
          />
          <Button
            variant="contained"
            onClick={() => {
              const n = Number(taskId);
              if (!Number.isNaN(n) && n > 0) load(n);
            }}
            disabled={loading}
          >
            Load Runs
          </Button>
          <Button
            variant="outlined"
            onClick={() => {
              const n = Number(taskId);
              if (!Number.isNaN(n) && n > 0) backfill(n);
            }}
            disabled={loading}
          >
            Backfill Labels
          </Button>
          {loading && <span style={{ marginLeft: 8, color: '#666' }}>Loading…</span>}
        </div>
        {error && (
          <div
            style={{
              marginTop: 12,
              padding: 12,
              backgroundColor: '#ffebee',
              border: '1px solid #ef5350',
              borderRadius: 4,
              color: '#c62828',
              fontSize: 14,
            }}
          >
            Failed to load runs: {error}
          </div>
        )}
      </div>

      {/* Results Section */}
      {runs && (
        <div>
          {runs.length === 0 ? (
            <div
              style={{
                padding: 16,
                backgroundColor: '#f5f5f5',
                borderRadius: 4,
                border: '1px solid #e0e0e0',
                color: '#666',
                textAlign: 'center',
              }}
            >
              No runs found for this scheduled task.
            </div>
          ) : (
            runs.map((grp) => (
              <div
                key={grp.schedule_id}
                style={{
                  marginBottom: 20,
                  backgroundColor: '#fff',
                  border: '1px solid #e0e0e0',
                  borderRadius: 4,
                  overflow: 'hidden',
                }}
              >
                {/* Parent Task Header */}
                <div
                  style={{
                    backgroundColor: '#e3f2fd',
                    borderBottom: '2px solid #1976d2',
                    padding: '12px 16px',
                  }}
                >
                  <div style={{ fontWeight: 700, fontSize: 14, color: '#1565c0', marginBottom: 4 }}>
                    Schedule #{grp.schedule_id}
                  </div>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))',
                      gap: 12,
                      fontSize: 12,
                      color: '#555',
                    }}
                  >
                    <div>
                      <strong>Start From:</strong> {grp.start_from || '—'}
                    </div>
                    <div>
                      <strong>Planned End At:</strong> {grp.planned_end_at || '—'}
                    </div>
                    <div>
                      <strong>Until:</strong> {grp.until || '—'}
                    </div>
                  </div>
                </div>

                {/* Child Tasks Table */}
                <div style={{ overflowX: 'auto' }}>
                  <table
                    style={{
                      width: '100%',
                      borderCollapse: 'collapse',
                      fontSize: 14,
                    }}
                  >
                    <thead>
                      <tr style={{ backgroundColor: '#f5f5f5' }}>
                        <th
                          style={{
                            textAlign: 'left',
                            padding: '12px 16px',
                            fontWeight: 600,
                            color: '#333',
                            borderBottom: '2px solid #ddd',
                          }}
                        >
                          Child Task
                        </th>
                        <th
                          style={{
                            textAlign: 'left',
                            padding: '12px 16px',
                            fontWeight: 600,
                            color: '#333',
                            borderBottom: '2px solid #ddd',
                          }}
                        >
                          Status
                        </th>
                        <th
                          style={{
                            textAlign: 'left',
                            padding: '12px 16px',
                            fontWeight: 600,
                            color: '#333',
                            borderBottom: '2px solid #ddd',
                          }}
                        >
                          Start Time
                        </th>
                        <th
                          style={{
                            textAlign: 'left',
                            padding: '12px 16px',
                            fontWeight: 600,
                            color: '#333',
                            borderBottom: '2px solid #ddd',
                          }}
                        >
                          Finish Time
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {grp.loops.map((lp, idx) => (
                        <tr
                          key={lp.task_id}
                          style={{
                            backgroundColor: idx % 2 === 0 ? '#fafafa' : '#fff',
                            borderBottom: '1px solid #eee',
                          }}
                        >
                          <td
                            style={{
                              padding: '12px 16px',
                              fontFamily: 'monospace',
                              color: '#1565c0',
                              fontSize: 13,
                            }}
                          >
                            {lp.task_id}
                          </td>
                          <td
                            style={{
                              padding: '12px 16px',
                              fontWeight: 500,
                              color: getStatusColor(lp.status),
                            }}
                          >
                            {lp.status || '—'}
                          </td>
                          <td style={{ padding: '12px 16px', color: '#555' }}>
                            {lp.unix_millis_start_time
                              ? new Date(lp.unix_millis_start_time).toLocaleString()
                              : '—'}
                          </td>
                          <td style={{ padding: '12px 16px', color: '#555' }}>
                            {lp.unix_millis_finish_time
                              ? new Date(lp.unix_millis_finish_time).toLocaleString()
                              : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </Paper>
  );
};
