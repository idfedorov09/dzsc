package pro.doczilla.dzsc.agent;

import org.zenframework.z8.server.runtime.AbstractRuntime;

public final class DzscAgentRuntime extends AbstractRuntime {
	public DzscAgentRuntime() {
		addRequest(new DzscAgentBridge.CLASS<DzscAgentBridge>(null));
	}
}
