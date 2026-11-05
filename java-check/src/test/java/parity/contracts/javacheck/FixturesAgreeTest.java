package parity.contracts.javacheck;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.networknt.schema.Error;
import com.networknt.schema.Schema;
import com.networknt.schema.SchemaRegistry;
import com.networknt.schema.dialect.Dialect;
import com.networknt.schema.dialect.Dialects;
import com.networknt.schema.keyword.AnnotationKeyword;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * networknt 3.0.8 must agree with contract-check (Rust) on every Kafka fixture: a value is
 * schema-valid exactly when its fixture doesn't list {@code schema} in {@code violates}.
 * {@code x-pii} and {@code x-reserved} are registered as annotation keywords (ruling S2).
 */
class FixturesAgreeTest {

    private static final Path ROOT = Path.of(System.getProperty("parity.contracts.dir", ".."));
    private static final JsonMapper JSON = JsonMapper.builder().build();

    private static Schema load(SchemaRegistry registry, String rel) throws IOException {
        try (InputStream in = Files.newInputStream(ROOT.resolve(rel))) {
            return registry.getSchema(in);
        }
    }

    @Test
    void everyKafkaFixtureAgreesWithItsDeclaredSchemaResult() throws IOException {
        Dialect dialect = Dialect.builder(Dialects.getDraft202012())
                .keyword(new AnnotationKeyword("x-pii"))
                .keyword(new AnnotationKeyword("x-reserved"))
                .build();
        SchemaRegistry registry = SchemaRegistry.withDialect(dialect);
        Map<String, Schema> schemas = Map.of(
                "events", load(registry, "schemas/events/interaction-events.v1.json"),
                "commands", load(registry, "schemas/commands/interaction-commands.v1.json"),
                "replies", load(registry, "schemas/commands/interaction-replies.v1.json"));
        List<String> disagreements = new ArrayList<>();
        int checked = 0;
        List<Path> files;
        try (Stream<Path> walk = Files.walk(ROOT.resolve("fixtures"))) {
            files = walk.filter(p -> p.toString().endsWith(".json")).sorted().toList();
        }
        for (Path file : files) {
            JsonNode fixture = JSON.readTree(file.toFile());
            if (!"kafka".equals(fixture.path("kind").asString()) || !fixture.path("record").has("value")) {
                continue;
            }
            String topic = fixture.path("record").path("topic").asString();
            String name = topic.equals("interaction.events") ? "events"
                    : topic.equals("interaction.commands") ? "commands" : "replies";
            List<Error> errors = schemas.get(name).validate(fixture.path("record").path("value"));
            boolean declared = false;
            for (JsonNode code : fixture.path("violates")) {
                declared |= "schema".equals(code.asString());
            }
            if (errors.isEmpty() == declared) {
                disagreements.add(ROOT.relativize(file) + ": networknt says " + (errors.isEmpty() ? "valid" : errors));
            }
            checked++;
        }
        assertTrue(checked >= 50, "expected the Kafka fixtures, found " + checked);
        assertEquals(List.of(), disagreements);
    }
}
