package org.horizonatlas.apex;

/**
 * Receives notification requests from Chrome so the website's push notifications are shown
 * as APEX notifications (with the APEX icon) instead of as Chrome notifications.
 */
public class DelegationService extends
        com.google.androidbrowserhelper.trusted.DelegationService {
    @Override
    public void onCreate() {
        super.onCreate();
    }
}
