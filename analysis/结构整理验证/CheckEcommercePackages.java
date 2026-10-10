import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.persistence.Entity;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.TreeSet;
import org.springframework.context.annotation.ClassPathScanningCandidateComponentProvider;
import org.springframework.core.env.StandardEnvironment;
import org.springframework.data.jpa.repository.JpaRepository;

/** 只检查包扫描与类型元数据，不创建应用上下文或连接数据库。 */
public class CheckEcommercePackages {
    public static void main(String[] args) throws Exception {
        JsonNode layout = new ObjectMapper().readTree(Files.readString(Path.of(args[0])));
        var environment = new StandardEnvironment();
        environment.setActiveProfiles("course-debug");
        var scanner = new ClassPathScanningCandidateComponentProvider(true);
        scanner.setEnvironment(environment);
        Set<String> components = new TreeSet<>();
        for (var bean : scanner.findCandidateComponents("com.teachdemo.ecommerce")) {
            components.add(bean.getBeanClassName().substring(bean.getBeanClassName().lastIndexOf('.') + 1));
        }
        Set<String> entities = new TreeSet<>();
        Set<String> repositories = new TreeSet<>();
        for (JsonNode name : layout.get("java_classes")) {
            Class<?> type = Class.forName(name.asText(), false, CheckEcommercePackages.class.getClassLoader());
            if (type.isAnnotationPresent(Entity.class)) entities.add(type.getSimpleName());
            if (type.isInterface() && JpaRepository.class.isAssignableFrom(type)) repositories.add(type.getSimpleName());
        }
        check(components, layout.get("spring_components"));
        check(entities, layout.get("jpa_entities"));
        check(repositories, layout.get("jpa_repositories"));
        System.out.printf("{\"spring_components\":%d,\"jpa_entities\":%d,\"jpa_repositories\":%d,\"database_calls\":0}%n",
                          components.size(), entities.size(), repositories.size());
    }

    private static void check(Set<String> actual, JsonNode expectedValues) {
        Set<String> expected = new TreeSet<>();
        expectedValues.forEach(value -> expected.add(value.asText()));
        if (!actual.equals(expected)) throw new AssertionError("Expected " + expected + "; got " + actual);
    }
}
