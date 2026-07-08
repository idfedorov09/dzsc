package pro.doczilla.dzsc.agent;

import java.io.File;
import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;

import javax.tools.Diagnostic;
import javax.tools.DiagnosticListener;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

import org.eclipse.core.runtime.CoreException;
import org.zenframework.z8.compiler.cmd.Main;
import org.zenframework.z8.compiler.error.BuildError;
import org.zenframework.z8.compiler.error.BuildMessage;
import org.zenframework.z8.compiler.error.BuildWarning;
import org.zenframework.z8.compiler.error.IBuildMessageConsumer;
import org.zenframework.z8.compiler.workspace.Project;
import org.zenframework.z8.compiler.workspace.ProjectProperties;
import org.zenframework.z8.compiler.workspace.Resource;
import org.zenframework.z8.server.base.file.Folders;
import org.zenframework.z8.server.engine.ApplicationServer;
import org.zenframework.z8.server.json.parser.JsonArray;
import org.zenframework.z8.server.json.parser.JsonObject;
import org.zenframework.z8.server.runtime.IObject;
import org.zenframework.z8.server.runtime.OBJECT;
import org.zenframework.z8.server.runtime.RMap;
import org.zenframework.z8.server.types.bool;
import org.zenframework.z8.server.types.guid;
import org.zenframework.z8.server.types.primary;
import org.zenframework.z8.server.types.string;

public class DzscAgentBridge extends OBJECT {
	private static final String DefaultClassName = "pro.doczilla.dzsc.agent.AgentTask";
	private static final String DefaultMethodName = "run";
	private static final String BlSources = "bl";
	private static final String JavaSources = "java";
	private static final String JavaClasses = "classes";

	public static class CLASS<T extends DzscAgentBridge> extends OBJECT.CLASS<T> {
		public CLASS(IObject container) {
			super(container);
			setJavaClass(DzscAgentBridge.class);
			setAttribute("request", "true");
			setDisplayName("DZSC Agent Runtime Bridge");
		}

		@Override
		public Object newObject(IObject container) {
			return new DzscAgentBridge(container);
		}
	}

	public DzscAgentBridge(IObject container) {
		super(container);
	}

	@Override
	public JsonArray.CLASS<? extends JsonArray> z8_getData(RMap<string, string> parameters) {
		JsonArray.CLASS<JsonArray> data = new JsonArray.CLASS<JsonArray>(this);
		data.get().add(execute(parameters));
		return data;
	}

	private JsonObject execute(RMap<string, string> parameters) {
		long started = java.lang.System.currentTimeMillis();
		JsonObject response = new JsonObject();
		String requestUserId = userId();
		boolean switchToSystem = !booleanParameter(parameters, "asCurrentUser", false);
		boolean userSwitched = false;

		try {
			String code = required(parameters, "code");
			String className = parameter(parameters, "className", DefaultClassName);
			String methodName = parameter(parameters, "methodName", DefaultMethodName);
			String input = parameter(parameters, "input", "{}");
			boolean keepWorkspace = booleanParameter(parameters, "keepWorkspace", false);

			validateClassName(className);
			validateMethodName(methodName);

			if(switchToSystem) {
				ApplicationServer.switchUser(ApplicationServer.getSystem());
				userSwitched = true;
			}

			String executionUserId = userId();
			AgentWorkspace workspace = new AgentWorkspace(className);

			try {
				workspace.clean();
				workspace.writeSource(code);
				CompileMessages messages = compile(workspace);
				Object result = invoke(workspace, className, methodName, input);

				response.set("ok", true);
				response.set("className", className);
				response.set("methodName", methodName);
				response.set("result", normalizeResult(result));
				response.set("requestUserId", requestUserId);
				response.set("executionUserId", executionUserId);
				response.set("workspace", workspace.root.getAbsolutePath());
				response.set("messages", messages.toJsonArray());
			} finally {
				if(!keepWorkspace)
					workspace.delete();
			}
		} catch(Throwable e) {
			Throwable root = unwrap(e);
			response.set("ok", false);
			response.set("error", root.getMessage() != null ? root.getMessage() : root.toString());
			response.set("exception", root.getClass().getName());
			response.set("stackTrace", stackTrace(root));
		} finally {
			if(userSwitched)
				ApplicationServer.restoreUser();
			response.set("durationMs", java.lang.System.currentTimeMillis() - started);
		}

		return response;
	}

	private CompileMessages compile(AgentWorkspace workspace) throws Exception {
		CompileMessages messages = new CompileMessages(workspace.javaSources);

		compileBl(workspace, messages);
		if(messages.getErrorCount() != 0)
			throw new AgentRuntimeException("BL compilation failed", messages);

		compileJava(workspace, messages);
		if(messages.getErrorCount() != 0)
			throw new AgentRuntimeException("Java compilation failed", messages);

		return messages;
	}

	private void compileBl(AgentWorkspace workspace, CompileMessages messages) throws Exception {
		File[] dependencies = dependencyFiles();

		ProjectProperties properties = new ProjectProperties(workspace.root);
		properties.setProjectName(ApplicationServer.getSchema() + "-dzsc-agent");
		properties.setSourcePaths(BlSources);
		properties.setOutputPath(JavaSources);
		properties.setRequiredPaths(dependencies);

		try {
			Project project = Main.initializeProject(properties);
			project.build(messages);
		} catch(CoreException e) {
			throw new AgentRuntimeException("Unable to initialize BL compiler", messages, e);
		}
	}

	private void compileJava(AgentWorkspace workspace, CompileMessages messages) throws Exception {
		JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
		if(compiler == null)
			throw new RuntimeException("Java compiler not found. Doczilla must run on a JDK, not a JRE.");

		List<File> javaFiles = new ArrayList<File>();
		collectJavaFiles(workspace.javaSources, javaFiles);
		if(javaFiles.isEmpty())
			throw new RuntimeException("BL compiler produced no Java files");

		workspace.javaClasses.mkdirs();
		StandardJavaFileManager fileManager = compiler.getStandardFileManager(null, null, null);
		try {
			List<String> options = Arrays.asList(
				"-classpath", java.lang.System.getProperty("java.class.path"),
				"-d", workspace.javaClasses.getAbsolutePath()
			);
			JavaCompiler.CompilationTask task = compiler.getTask(
				null,
				fileManager,
				messages,
				options,
				null,
				fileManager.getJavaFileObjectsFromFiles(javaFiles)
			);
			if(!task.call())
				throw new AgentRuntimeException("Java compilation failed", messages);
		} finally {
			fileManager.close();
		}
	}

	private Object invoke(AgentWorkspace workspace, String className, String methodName, String input) throws Exception {
		URL[] urls = new URL[] { workspace.javaClasses.toURI().toURL() };
		URLClassLoader loader = new URLClassLoader(urls, DzscAgentBridge.class.getClassLoader());
		try {
			Class<?> objectClass = loader.loadClass(className + "$CLASS");
			Object cls = objectClass.getConstructor(IObject.class).newInstance((IObject)null);
			Object instance = objectClass.getMethod("newInstance").invoke(cls);
			Method method = findRunMethod(instance.getClass(), methodName);

			if(method.getParameterTypes().length == 0)
				return method.invoke(instance);

			return method.invoke(instance, new string(input != null ? input : ""));
		} finally {
			loader.close();
		}
	}

	private Method findRunMethod(Class<?> cls, String methodName) throws NoSuchMethodException {
		String javaMethodName = "z8_" + methodName;
		try {
			return cls.getMethod(javaMethodName, string.class);
		} catch(NoSuchMethodException ignored) {
			return cls.getMethod(javaMethodName);
		}
	}

	private File[] dependencyFiles() {
		File dependencies = new File(Folders.ApplicationPath, "dzsc-agent/dependencies");
		File[] files = dependencies.listFiles(file -> file.isFile() && (file.getName().endsWith(".zip") || file.getName().endsWith(".blar")));
		if(files == null || files.length == 0)
			throw new RuntimeException("DZSC agent runtime dependencies not found: " + dependencies.getAbsolutePath());

		Arrays.sort(files, Comparator.comparing(File::getName));
		boolean hasLang = false;
		for(File file : files) {
			if(file.getName().startsWith("org.zenframework.z8.lang-") && file.getName().endsWith(".zip"))
				hasLang = true;
			if("z8.zip".equals(file.getName()))
				throw new RuntimeException("z8.zip must not be placed into dzsc-agent/dependencies");
		}
		if(!hasLang)
			throw new RuntimeException("org.zenframework.z8.lang zip not found in " + dependencies.getAbsolutePath());
		return files;
	}

	private static void collectJavaFiles(File folder, List<File> files) {
		File[] children = folder.listFiles();
		if(children == null)
			return;

		for(File child : children) {
			if(child.isDirectory())
				collectJavaFiles(child, files);
			else if(child.getName().endsWith(".java"))
				files.add(child);
		}
	}

	private static Object normalizeResult(Object result) {
		if(result == null)
			return null;
		if(result instanceof primary)
			return result.toString();
		if(result instanceof JsonObject || result instanceof JsonArray)
			return result;
		if(result instanceof org.zenframework.z8.server.runtime.CLASS) {
			Object value = ((org.zenframework.z8.server.runtime.CLASS<?>)result).get();
			if(value instanceof JsonObject || value instanceof JsonArray)
				return value;
			return value != null ? value.toString() : null;
		}
		return String.valueOf(result);
	}

	private static String parameter(RMap<string, string> parameters, String name, String fallback) {
		string value = parameters.get(new string(name));
		return value != null ? value.get() : fallback;
	}

	private static String required(RMap<string, string> parameters, String name) {
		String value = parameter(parameters, name, null);
		if(value == null || value.trim().isEmpty())
			throw new IllegalArgumentException("Missing required parameter: " + name);
		return value;
	}

	private static boolean booleanParameter(RMap<string, string> parameters, String name, boolean fallback) {
		String value = parameter(parameters, name, null);
		return value != null ? Boolean.parseBoolean(value) : fallback;
	}

	private static void validateClassName(String className) {
		if(className == null || !className.matches("[A-Za-z_$][A-Za-z0-9_$]*(\\.[A-Za-z_$][A-Za-z0-9_$]*)*"))
			throw new IllegalArgumentException("Invalid className: " + className);
	}

	private static void validateMethodName(String methodName) {
		if(methodName == null || !methodName.matches("[A-Za-z_$][A-Za-z0-9_$]*"))
			throw new IllegalArgumentException("Invalid methodName: " + methodName);
	}

	private static Throwable unwrap(Throwable throwable) {
		if(throwable instanceof InvocationTargetException && ((InvocationTargetException)throwable).getTargetException() != null)
			return unwrap(((InvocationTargetException)throwable).getTargetException());
		if(throwable instanceof AgentRuntimeException && throwable.getCause() != null)
			return throwable;
		return throwable;
	}

	private static String stackTrace(Throwable throwable) {
		StringWriter buffer = new StringWriter();
		throwable.printStackTrace(new PrintWriter(buffer));
		return buffer.toString();
	}

	private static String userId() {
		guid id = ApplicationServer.getUser().getId();
		return id != null ? id.toString() : null;
	}

	private static final class AgentWorkspace {
		final String className;
		final File root;
		final File blSources;
		final File javaSources;
		final File javaClasses;

		AgentWorkspace(String className) {
			this.className = className;
			String schema = sanitize(ApplicationServer.getSchema());
			String runId = UUID.randomUUID().toString().replace("-", "");
			this.root = new File(new File(Folders.WorkingPath, "dzsc-agent/workspaces"), schema + "/" + runId);
			this.blSources = new File(root, BlSources);
			this.javaSources = new File(root, JavaSources);
			this.javaClasses = new File(root, JavaClasses);
		}

		void clean() throws IOException {
			delete();
			mkdirs(blSources);
			mkdirs(javaSources);
			mkdirs(javaClasses);
		}

		void writeSource(String code) throws IOException {
			File source = new File(blSources, className.replace('.', '/') + ".bl");
			mkdirs(source.getParentFile());
			Files.write(source.toPath(), code.getBytes(StandardCharsets.UTF_8));
		}

		void delete() throws IOException {
			deleteRecursively(root);
		}

		private static String sanitize(String value) {
			return value == null || value.isEmpty() ? "default" : value.replaceAll("[^A-Za-z0-9_.-]", "_");
		}

		private static void mkdirs(File folder) throws IOException {
			if(!folder.exists() && !folder.mkdirs())
				throw new IOException("Couldn't create folder: " + folder.getAbsolutePath());
		}
	}

	private static void deleteRecursively(File file) throws IOException {
		if(file == null || !file.exists())
			return;

		if(file.isDirectory()) {
			File[] children = file.listFiles();
			if(children != null) {
				for(File child : children)
					deleteRecursively(child);
			}
		}

		if(!file.delete() && file.exists())
			throw new IOException("Couldn't delete: " + file.getAbsolutePath());
	}

	private static final class CompileMessages implements IBuildMessageConsumer, DiagnosticListener<JavaFileObject> {
		private final File javaSources;
		private final List<String> messages = new ArrayList<String>();
		private int errors = 0;
		private int warnings = 0;

		CompileMessages(File javaSources) {
			this.javaSources = javaSources;
		}

		@Override
		public int getErrorCount() {
			return errors;
		}

		@Override
		public int getWarningCount() {
			return warnings;
		}

		@Override
		public void consume(BuildMessage message) {
			addBuildMessage(null, message);
		}

		@Override
		public void report(Resource resource, BuildMessage[] buildMessages) {
			String path = resource != null && resource.getSourceRelativePath() != null ? resource.getSourceRelativePath().toString() : null;
			for(BuildMessage message : buildMessages)
				addBuildMessage(path, message);
		}

		@Override
		public void clearMessages(Resource resource) {
		}

		@Override
		public void report(Diagnostic<? extends JavaFileObject> diagnostic) {
			if(diagnostic.getKind() == Diagnostic.Kind.ERROR)
				errors++;
			else if(diagnostic.getKind() == Diagnostic.Kind.WARNING || diagnostic.getKind() == Diagnostic.Kind.MANDATORY_WARNING)
				warnings++;

			String source = diagnostic.getSource() != null ? diagnostic.getSource().getName() : null;
			if(source != null && source.startsWith(javaSources.getAbsolutePath()))
				source = source.substring(javaSources.getAbsolutePath().length() + 1);
			messages.add("JAVA " + diagnostic.getKind() + formatPosition(diagnostic.getLineNumber(), diagnostic.getColumnNumber()) + ": " + (source != null ? source + ": " : "") + diagnostic.getMessage(null));
		}

		JsonArray toJsonArray() {
			JsonArray array = new JsonArray();
			for(String message : messages)
				array.add(message);
			return array;
		}

		private void addBuildMessage(String path, BuildMessage message) {
			if(message instanceof BuildError)
				errors++;
			else if(message instanceof BuildWarning)
				warnings++;

			String prefix = message instanceof BuildError ? "BL ERROR" : message instanceof BuildWarning ? "BL WARNING" : "BL INFO";
			String location = path != null ? path + ": " : "";
			messages.add(prefix + ": " + location + message.format());
		}

		private static String formatPosition(long line, long column) {
			return line > 0 ? " (" + line + ", " + column + ")" : "";
		}
	}

	private static final class AgentRuntimeException extends Exception {
		private static final long serialVersionUID = 1L;

		AgentRuntimeException(String message, CompileMessages messages) {
			super(message + ": " + messages.toJsonArray().toString());
		}

		AgentRuntimeException(String message, CompileMessages messages, Throwable cause) {
			super(message + ": " + messages.toJsonArray().toString(), cause);
		}
	}
}
