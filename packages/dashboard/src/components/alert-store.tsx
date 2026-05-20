import { TortoiseContribPydanticCreatorApiServerModelsTortoiseModelsAlertsAlertLeaf as Alert } from 'api-client';
import { AppEvents } from './app-events';
import React from 'react';
import { RmfAppContext } from './rmf-app';
import { Subscription } from 'rxjs';
import { TaskAlertDialog } from './tasks/task-alert';
import { AppControllerContext } from './app-contexts';
import { CriticalAlert } from './critical-alert';

// This needs to match the enums provided for the Alert model, as it is not
// provided via the api-client since tortoise's pydantic_model_creator is used.
enum AlertCategory {
  Default = 'default',
  Task = 'task',
  Fleet = 'fleet',
  Robot = 'robot',
  FireCritical = 'fire_critical',
  FireWarning = 'fire_warning',
  SecurityThreat = 'security_threat',
  Patrol = 'patrol',
  Semantic = 'semantic',
}

export interface SemanticAlert {
  id: string;
  message: string;
  category: string;
  severity: string;
  source_type: string;
  timestamp: number;
  dedup_key?: string;
  robot_name?: string;
  nearest_waypoint?: string;
  x?: number;
  y?: number;
}

export const AlertStore = React.memo(() => {
  const rmf = React.useContext(RmfAppContext);
  const appController = React.useContext(AppControllerContext);
  const [taskAlerts, setTaskAlerts] = React.useState<Record<string, Alert>>({});
  const [criticalAlerts, setCriticalAlerts] = React.useState<Record<string, SemanticAlert>>({});
  const [shownCriticalAlerts, setShownCriticalAlerts] = React.useState<Map<string, number>>(
    new Map(),
  );
  const enableTaskAlertDialog = false;
  const acknowledgedAlertsRef = React.useRef<Set<string>>(new Set());

  const CRITICAL_ALERT_DEDUP_WINDOW_MS = 30000; // 30 seconds

  // Check if we should show a critical alert (deduplication)
  const shouldShowCriticalAlert = (dedupKey: string): boolean => {
    const lastShownTime = shownCriticalAlerts.get(dedupKey);
    const now = Date.now();

    if (lastShownTime && now - lastShownTime < CRITICAL_ALERT_DEDUP_WINDOW_MS) {
      return false;
    }
    return true;
  };

  // Mark a critical alert as shown
  const markAlertAsShown = (dedupKey: string) => {
    setShownCriticalAlerts((prev) => {
      const newMap = new Map(prev);
      newMap.set(dedupKey, Date.now());
      return newMap;
    });
  };
  const parseFireMessage = (message: string) => {
    // Example:
    //  TinyRobot1 FIRE DETECTED at cleaner_pantry (73.0, -34.5)

    const robotMatch = message.match(/🔥\s*(.*?)\s*FIRE/i);
    const locationMatch = message.match(/at\s+([^(]+)/i);
    const coordMatch = message.match(/\(([-\d.]+),\s*([-\d.]+)\)/);

    return {
      robot_name: robotMatch?.[1]?.trim(),
      location: locationMatch?.[1]?.trim(),
      x: coordMatch ? parseFloat(coordMatch[1]) : undefined,
      y: coordMatch ? parseFloat(coordMatch[2]) : undefined,
    };
  };

  const categorizeAndPushAlerts = (alert: Alert) => {
    // Handle semantic alerts from /rmf_demo_alerts (passed through /alerts API)
    const alertAny = alert as any;
    const sourceType = alertAny.source_type || alertAny.source;
    const category = alertAny.category;
    const severity = alertAny.severity;
    const dedupKey = alertAny.dedup_key;
    const normalizedCategory = (category || '').toLowerCase();
    if (dedupKey && acknowledgedAlertsRef.current.has(dedupKey)) {
      return;
    }

    if (
      sourceType === 'semantic' ||
      sourceType === 'patrol' ||
      sourceType === 'perception_bridge'
    ) {
      // Critical alerts: fire_critical, security_threat
      if (
        normalizedCategory === AlertCategory.FireCritical ||
        normalizedCategory === AlertCategory.FireWarning ||
        normalizedCategory === AlertCategory.SecurityThreat
      ) {
        // Deduplication check
        if (dedupKey && !shouldShowCriticalAlert(dedupKey)) {
          return;
        }

        if (dedupKey) {
          markAlertAsShown(dedupKey);
        }

        // Add to persistent critical alerts
        const semanticAlert: SemanticAlert = {
          id: alert.id,
          message: alertAny.alert_id || '',
          category: category,
          severity: severity || 'critical',
          source_type: sourceType,
          timestamp: Date.now(),
          dedup_key: dedupKey,
          robot_name: alertAny.robot_name,
          nearest_waypoint: alertAny.level_name,

          x: alertAny.robot_position?.x ?? alertAny.obstacle_position?.x,
          y: alertAny.robot_position?.y ?? alertAny.obstacle_position?.y,
        };

        setCriticalAlerts((prev) => ({
          ...prev,
          [alert.id]: semanticAlert,
        }));
      }
      // Warning and info alerts: show as temporary snackbars
      else if (
        category === AlertCategory.FireWarning ||
        category === AlertCategory.Patrol ||
        severity === 'warning' ||
        severity === 'info'
      ) {
        const message = alertAny.alert_id || '';
        const severity_level: 'error' | 'warning' | 'info' | 'success' =
          category === AlertCategory.FireWarning ? 'warning' : 'info';
        const duration = category === AlertCategory.FireWarning ? 4000 : 3000;

        appController?.showAlert(severity_level, message, duration);
      }
      // All other semantic alerts as info snackbars
      else {
        const message = alertAny.alert_id || '';
        appController?.showAlert('info', message, 3000);
      }
    }
    // Handle traditional task alerts
    else if (!enableTaskAlertDialog) {
      return;
    } else if (alert.category === AlertCategory.Task) {
      setTaskAlerts((prev) => {
        const filteredTaskAlerts = Object.fromEntries(
          Object.entries(prev).filter(([key]) => key !== alert.original_id),
        );
        filteredTaskAlerts[alert.id] = alert;
        return filteredTaskAlerts;
      });
    }
  };

  React.useEffect(() => {
    const subs: Subscription[] = [];
    subs.push(
      AppEvents.alertListOpenedAlert.subscribe((alert) => {
        if (alert) {
          categorizeAndPushAlerts(alert);
        }
      }),
    );
    return () => subs.forEach((s) => s.unsubscribe());
  }, []);

  React.useEffect(() => {
    if (!rmf) {
      return;
    }
    const sub = rmf.alertObsStore.subscribe(async (alert) => {
      categorizeAndPushAlerts(alert);
      AppEvents.refreshAlert.next();
    });
    return () => sub.unsubscribe();
  }, [rmf]);

  const removeTaskAlert = (id: string) => {
    const filteredTaskAlerts = Object.fromEntries(
      Object.entries(taskAlerts).filter(([key]) => key !== id),
    );
    setTaskAlerts(filteredTaskAlerts);
  };

  const removeCriticalAlert = (id: string, dedupKey?: string) => {
    if (dedupKey) {
      acknowledgedAlertsRef.current.add(dedupKey);
    }

    setCriticalAlerts((prev) => {
      const copy = { ...prev };
      delete copy[id];
      return copy;
    });

    AppEvents.removeCriticalAlert.next(id);
  };

  // Notify app-base whenever critical alerts change
  React.useEffect(() => {
    AppEvents.criticalAlertListUpdated.next(criticalAlerts);
  }, [criticalAlerts]);

  return (
    <>
      {/* Task Alerts */}
      {Object.values(taskAlerts).map((alert) => {
        const removeThisAlert = () => {
          removeTaskAlert(alert.id);
        };
        return <TaskAlertDialog key={alert.id} alert={alert} removeAlert={removeThisAlert} />;
      })}

      {/* Critical Alerts */}
      {Object.values(criticalAlerts).map((alert) => {
        return (
          <CriticalAlert
            key={alert.id}
            id={alert.id}
            message={alert.message}
            category={alert.category}
            severity={alert.severity}
            timestamp={alert.timestamp}
            robot_name={alert.robot_name}
            nearest_waypoint={alert.nearest_waypoint}
            x={alert.x}
            y={alert.y}
            onAcknowledge={() => removeCriticalAlert(alert.id, alert.dedup_key)}
          />
        );
      })}
    </>
  );
});
