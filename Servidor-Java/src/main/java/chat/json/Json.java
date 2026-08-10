package chat.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Lector y escritor de JSON mínimo, sin dependencias externas.
 *
 * <p>Cubre exactamente lo que el protocolo del chat necesita: objetos, arreglos,
 * cadenas, números, booleanos y {@code null}. No pretende ser un parser completo.
 */
public final class Json {

    private Json() {
    }

    // ---------------------------------------------------------------- escritura

    /** Serializa un valor a JSON. Acepta Map, List, String, Number, Boolean y null. */
    public static String write(Object value) {
        StringBuilder out = new StringBuilder();
        writeValue(value, out);
        return out.toString();
    }

    private static void writeValue(Object value, StringBuilder out) {
        switch (value) {
            case null -> out.append("null");
            case String s -> writeString(s, out);
            case Boolean b -> out.append(b);
            case Number n -> out.append(n);
            case Map<?, ?> map -> writeObject(map, out);
            case List<?> list -> writeArray(list, out);
            default -> writeString(String.valueOf(value), out);
        }
    }

    private static void writeObject(Map<?, ?> map, StringBuilder out) {
        out.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (entry.getValue() == null) {
                continue;   // los campos que no aplican se omiten del JSON
            }
            if (!first) {
                out.append(',');
            }
            first = false;
            writeString(String.valueOf(entry.getKey()), out);
            out.append(':');
            writeValue(entry.getValue(), out);
        }
        out.append('}');
    }

    private static void writeArray(List<?> list, StringBuilder out) {
        out.append('[');
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) {
                out.append(',');
            }
            writeValue(list.get(i), out);
        }
        out.append(']');
    }

    /**
     * Escapa la cadena según JSON. Es lo que garantiza que un salto de línea dentro
     * del texto de un mensaje viaje como {@code \n} de dos caracteres y no rompa el
     * delimitador del framing.
     */
    private static void writeString(String text, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    // ------------------------------------------------------------------ lectura

    /** Se lanza cuando el texto recibido no es JSON válido. */
    public static final class ParseException extends Exception {
        public ParseException(String message) {
            super(message);
        }
    }

    /** Interpreta un objeto JSON. Falla si el valor de más alto nivel no es un objeto. */
    public static Map<String, Object> readObject(String text) throws ParseException {
        Parser parser = new Parser(text);
        parser.skipWhitespace();
        Object value = parser.readValue();
        parser.skipWhitespace();
        if (!parser.atEnd()) {
            throw new ParseException("Sobran caracteres después del objeto JSON");
        }
        if (!(value instanceof Map)) {
            throw new ParseException("Se esperaba un objeto JSON");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> object = (Map<String, Object>) value;
        return object;
    }

    private static final class Parser {

        private final String text;
        private int pos;

        Parser(String text) {
            this.text = text;
        }

        boolean atEnd() {
            return pos >= text.length();
        }

        void skipWhitespace() {
            while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) {
                pos++;
            }
        }

        Object readValue() throws ParseException {
            skipWhitespace();
            if (atEnd()) {
                throw new ParseException("Fin de entrada inesperado");
            }
            char c = text.charAt(pos);
            return switch (c) {
                case '{' -> readObjectValue();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> readNumber();
            };
        }

        private Map<String, Object> readObjectValue() throws ParseException {
            expect('{');
            Map<String, Object> result = new LinkedHashMap<>();
            skipWhitespace();
            if (peek() == '}') {
                pos++;
                return result;
            }
            while (true) {
                skipWhitespace();
                String key = readString();
                skipWhitespace();
                expect(':');
                result.put(key, readValue());
                skipWhitespace();
                char next = peek();
                if (next == ',') {
                    pos++;
                    continue;
                }
                if (next == '}') {
                    pos++;
                    return result;
                }
                throw new ParseException("Se esperaba ',' o '}' en la posición " + pos);
            }
        }

        private List<Object> readArray() throws ParseException {
            expect('[');
            List<Object> result = new ArrayList<>();
            skipWhitespace();
            if (peek() == ']') {
                pos++;
                return result;
            }
            while (true) {
                result.add(readValue());
                skipWhitespace();
                char next = peek();
                if (next == ',') {
                    pos++;
                    continue;
                }
                if (next == ']') {
                    pos++;
                    return result;
                }
                throw new ParseException("Se esperaba ',' o ']' en la posición " + pos);
            }
        }

        private String readString() throws ParseException {
            expect('"');
            StringBuilder out = new StringBuilder();
            while (true) {
                if (atEnd()) {
                    throw new ParseException("Cadena sin cerrar");
                }
                char c = text.charAt(pos++);
                if (c == '"') {
                    return out.toString();
                }
                if (c != '\\') {
                    out.append(c);
                    continue;
                }
                if (atEnd()) {
                    throw new ParseException("Escape incompleto");
                }
                char escaped = text.charAt(pos++);
                switch (escaped) {
                    case '"' -> out.append('"');
                    case '\\' -> out.append('\\');
                    case '/' -> out.append('/');
                    case 'n' -> out.append('\n');
                    case 'r' -> out.append('\r');
                    case 't' -> out.append('\t');
                    case 'b' -> out.append('\b');
                    case 'f' -> out.append('\f');
                    case 'u' -> {
                        if (pos + 4 > text.length()) {
                            throw new ParseException("Escape unicode incompleto");
                        }
                        String hex = text.substring(pos, pos + 4);
                        pos += 4;
                        try {
                            out.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException e) {
                            throw new ParseException("Escape unicode inválido: " + hex);
                        }
                    }
                    default -> throw new ParseException("Escape desconocido: \\" + escaped);
                }
            }
        }

        private Boolean readBoolean() throws ParseException {
            if (text.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (text.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new ParseException("Valor booleano inválido en la posición " + pos);
        }

        private Object readNull() throws ParseException {
            if (text.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new ParseException("Valor inválido en la posición " + pos);
        }

        private Double readNumber() throws ParseException {
            int start = pos;
            while (pos < text.length() && "+-.eE0123456789".indexOf(text.charAt(pos)) >= 0) {
                pos++;
            }
            if (start == pos) {
                throw new ParseException("Valor inesperado en la posición " + pos);
            }
            try {
                return Double.valueOf(text.substring(start, pos));
            } catch (NumberFormatException e) {
                throw new ParseException("Número inválido: " + text.substring(start, pos));
            }
        }

        private char peek() throws ParseException {
            if (atEnd()) {
                throw new ParseException("Fin de entrada inesperado");
            }
            return text.charAt(pos);
        }

        private void expect(char expected) throws ParseException {
            if (atEnd() || text.charAt(pos) != expected) {
                throw new ParseException("Se esperaba '" + expected + "' en la posición " + pos);
            }
            pos++;
        }
    }

    // ------------------------------------------------------------------ ayudas

    /**
     * Devuelve el campo como texto recortado, o {@code null} si falta o está vacío.
     * Un número se normaliza a texto: el contrato los define como cadenas, pero un
     * cliente podría enviar un identificador numérico y no debe perderse en silencio.
     */
    public static String string(Map<String, Object> object, String key) {
        Object value = object.get(key);
        if (value instanceof String text) {
            String trimmed = text.trim();
            return trimmed.isEmpty() ? null : trimmed;
        }
        if (value instanceof Number number) {
            if (number.doubleValue() == Math.floor(number.doubleValue())) {
                return String.valueOf(number.longValue());
            }
            return String.valueOf(number);
        }
        return null;
    }
}
